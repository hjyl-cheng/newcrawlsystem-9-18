#!/usr/bin/python3
"""CDC candidate fencing for this fixed three-node PG17/Patroni cluster.

PG still owns slot synchronization; Patroni still owns leader election/fencing.
Only the current DCS leader can publish a persistent CDC candidate policy.
Expand the physical WAL barrier before admitting a candidate; fence a candidate
in DCS before removing it from the barrier. pre_promote enforces that policy.
"""
import argparse
import base64
import concurrent.futures
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import ssl
import time
import urllib.request
import uuid

NODES={'s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
SLOT='crawl_cdc_validation'
STATE=Path('/var/lib/crawl-cdc-guard')
CONFIG=Path('/etc/crawl-patroni/patroni.yml')
PREFIX='/service/crawl-pg/'

def require(value,message):
    if not value:raise RuntimeError(message)

def lsn(value):
    if not value:return -1
    hi,lo=value.split('/');return (int(hi,16)<<32)+int(lo,16)

def usable(slot,standby=True):
    return bool(slot and slot['failover'] and not slot['temporary'] and not slot['invalid']
        and slot['database']=='crawler' and slot['plugin']=='pgoutput'
        and slot['wal_status'] in ('reserved','extended') and slot['confirmed'] and slot['restart']
        and (not standby or slot['synced']))

def emit(event,**fields):
    print(json.dumps({'time':time.time(),'event':event,**fields}),flush=True)

def b64(value):return base64.b64encode(value.encode()).decode()

class DCS:
    def __init__(self,cfg):
        self.cfg=cfg['etcd3'];self.token=None
        self.context=ssl.create_default_context(cafile=self.cfg['cacert'])
        self.context.load_cert_chain(self.cfg['cert'],self.cfg['key'])
        self.hosts=self.cfg['hosts'].split(',')

    def request(self,host,path,body,auth=True):
        headers={'Content-Type':'application/json'}
        if auth:headers['Authorization']=self.token
        req=urllib.request.Request('https://'+host+'/v3'+path,data=json.dumps(body).encode(),headers=headers)
        with urllib.request.urlopen(req,context=self.context,timeout=1.5) as response:
            result=json.load(response)
        require('error' not in result,'DCS RPC failed')
        return result

    def rpc(self,path,body):
        for host in self.hosts:
            try:
                if not self.token:
                    self.token=self.request(host,'/auth/authenticate',{'name':self.cfg['username'],'password':self.cfg['password']},False)['token']
                return self.request(host,path,body)
            except Exception:self.token=None
        raise RuntimeError('No linearizable DCS response')

    def snapshot(self):
        keys=['leader','cdc_guard','initialize']
        requests=[{'request_range':{'key':b64(PREFIX+k),'serializable':False}} for k in keys]
        requests.append({'request_range':{'key':b64(PREFIX+'members/'),'range_end':b64(PREFIX+'members0'),'serializable':False}})
        result=self.rpc('/kv/txn',{'success':requests})
        values={}
        for key,response in zip(keys,result['responses']):
            rows=response['response_range'].get('kvs',[])
            values[key]={'value':base64.b64decode(rows[0]['value']).decode(),'revision':rows[0]['mod_revision']} if rows else {'value':None,'revision':'0'}
        values['policy']=json.loads(values['cdc_guard']['value']) if values['cdc_guard']['value'] else None
        values['members']={base64.b64decode(row['key']).decode().rsplit('/',1)[-1]:json.loads(base64.b64decode(row['value']).decode()) for row in result['responses'][-1]['response_range'].get('kvs',[])}
        return values

    def publish(self,name,snapshot,policy):
        # Exact leader revision also fences a later acquisition with the same name.
        comparisons=[{'key':b64(PREFIX+'leader'),'target':'VALUE','value':b64(name)},
            {'key':b64(PREFIX+'leader'),'target':'MOD','mod_revision':snapshot['leader']['revision']},
            {'key':b64(PREFIX+'cdc_guard'),'target':'MOD','mod_revision':snapshot['cdc_guard']['revision']}]
        return self.rpc('/kv/txn',{'compare':comparisons,'success':[{'request_put':{'key':b64(PREFIX+'cdc_guard'),'value':b64(json.dumps(policy,sort_keys=True))}}]}).get('succeeded',False)

class Guard:
    def __init__(self):
        self.cfg=json.loads(CONFIG.read_text());self.name=self.cfg['name']
        require(self.name in NODES,'Unexpected member')
        self.dcs=DCS(self.cfg);self.last_tick=0

    def query(self,statement,args=(),node=None,db='postgres'):
        import psycopg2
        options={'dbname':db,'connect_timeout':2,'options':'-c statement_timeout=2000 -c lock_timeout=1000'}
        if node and node!=self.name:
            auth=self.cfg['postgresql']['authentication']['replication']
            require(auth.get('sslmode')=='verify-full' and auth.get('sslrootcert'),
                'Replication SQL identity must configure verify-full and a trusted CA')
            options.update(host=NODES[node],user=auth['username'],password=auth['password'],
                sslmode=auth['sslmode'],sslrootcert=auth['sslrootcert'])
        else:options.update(host='/var/run/postgresql',user='postgres')
        connection=psycopg2.connect(**options)
        try:
            connection.autocommit=True
            with connection.cursor() as cur:
                cur.execute(statement,args)
                return cur.fetchall() if cur.description else []
        finally:connection.close()

    def state(self,node=None):
        return self.query("""SELECT json_build_object('recovery',pg_is_in_recovery(),
          'replay',pg_last_wal_replay_lsn(),'sender',(SELECT sender_host FROM pg_stat_wal_receiver),
          'slot',(SELECT json_build_object('active',active,'failover',failover,'synced',synced,
            'temporary',temporary,'invalid',invalidation_reason,'database',database,'plugin',plugin,
            'wal_status',wal_status,'confirmed',confirmed_flush_lsn,'restart',restart_lsn)
            FROM pg_replication_slots WHERE slot_name=%s))""",(SLOT,),node)[0][0]

    def barrier(self):
        value=self.query('SHOW synchronized_standby_slots')[0][0]
        return set(n.strip() for n in value.split(',') if n.strip())

    def set_barrier(self,names):
        require(bool(names) and set(names)<=set(NODES)-{self.name},'Never install an empty/unknown CDC barrier')
        if self.barrier()==set(names):return
        # PostgreSQL auto.conf overrides Patroni's conservative startup default.
        self.query('ALTER SYSTEM SET synchronized_standby_slots = %s',(','.join(sorted(names)),))
        self.query('SELECT pg_reload_conf()')
        for _ in range(15):
            if self.barrier()==set(names):return
            time.sleep(.1)
        raise RuntimeError('CDC barrier reload was not observed')

    def peers(self):
        streams={row[0]:row[1] for row in self.query("SELECT s.slot_name,host(r.client_addr) FROM pg_stat_replication r JOIN pg_replication_slots s ON s.active_pid=r.pid WHERE s.slot_type='physical' AND r.state='streaming'")}
        def read(name):
            try:
                state=self.state(name)
                state['streaming_from_current_primary']=streams.get(name)==NODES[name]
                return name,state
            except Exception:return name,None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            return dict(executor.map(read,[n for n in NODES if n!=self.name]))

    def candidate(self,state):
        return bool(state and state['recovery'] and state.get('streaming_from_current_primary') and usable(state['slot']))

    def primary(self,snapshot,local):
        require(snapshot['leader']['value']==self.name,'Local primary does not own DCS leader key')
        require(usable(local['slot'],False),'Primary CDC slot needs operator inspection')
        system_id=str(self.query('SELECT system_identifier FROM pg_control_system()')[0][0])
        require(snapshot['initialize']['value']==system_id,'DCS/PG system identifier mismatch')
        peers=self.peers();desired={name for name,state in peers.items() if self.candidate(state)
            and name in snapshot['members'] and not snapshot['members'][name].get('tags',{}).get('nofailover')}
        old=snapshot['policy']
        if not desired:
            # No safe candidate: keep existing durable fence and freeze on both peers.
            self.set_barrier(set(NODES)-{self.name})
            self.progress_pending(peers)
            return {'state':'WAITING_FOR_SAFE_STANDBY','candidates':[]}
        same=bool(old and old['owner']==self.name and old['system_id']==system_id and set(old['allowed'])==desired)
        if not same:
            # Stage 1: adding a node can only make the current barrier stricter.
            self.set_barrier(self.barrier()|desired)
            floor=self.query('SELECT pg_current_wal_flush_lsn()')[0][0]
            # All candidate WAL must predate the membership admission, including
            # events that Kafka may already have received before this expansion.
            peers=self.peers()
            if not all(self.candidate(peers.get(n)) and lsn(peers[n]['replay'])>=lsn(floor) for n in desired):
                return {'state':'WAITING_FOR_ADMISSION_WAL','candidates':sorted(desired),'floor':floor}
            new={'version':1,'owner':self.name,'allowed':sorted(desired),'floor':floor,'system_id':system_id,'generation':uuid.uuid4().hex}
            require(self.dcs.publish(self.name,snapshot,new),'DCS policy compare-and-swap rejected')
            emit('policy_published',**new)
            old=new
        # Stage 2: only after durable exclusion may an unavailable peer be removed.
        # A leadership change between the CAS and reload remains fenced by Patroni.
        require(self.dcs.snapshot()['leader']['value']==self.name,'Leadership changed before barrier reduction')
        self.set_barrier(desired)
        self.progress_pending(peers)
        return {'state':'READY','candidates':sorted(desired),'generation':old['generation']}

    def progress_pending(self,peers):
        # Native slot initialization can need catalog/WAL advancement. These are
        # bounded, real commits in the infrastructure-only outbox, never LSN skips.
        pending=[n for n,s in peers.items() if s and s['recovery'] and (not s['slot'] or s['slot']['temporary'] or not s['slot']['synced'])]
        if not pending or time.monotonic()-self.last_tick<60:return
        self.last_tick=time.monotonic()
        require(self.dcs.snapshot()['leader']['value']==self.name,'Not leader for recovery marker')
        self.query('CHECKPOINT')
        event_id=str(uuid.uuid4())
        self.query("INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES(%s,'infra','cdc-guard','SlotRecovery',%s::jsonb)",
            (event_id,json.dumps({'event_id':event_id,'run_id':'guard-recovery','channel_id':'cdc-guard','pending':pending})),db='crawler')
        emit('native_sync_progress_marker',nodes=pending,event_id=event_id)

    def standby(self,snapshot,local):
        slot=local['slot'];policy=snapshot['policy'];leader=snapshot['leader']['value']
        if slot and slot['failover'] and not slot['synced']:
            # The promotion hook rejects this slot even before policy exclusion.
            require(policy and policy['owner']==leader and self.name not in policy['allowed'],'Standby is not durably excluded')
            require(policy['system_id']==snapshot['initialize']['value']==str(self.query('SELECT system_identifier FROM pg_control_system()')[0][0]),'Repair system identifier mismatch')
            require(leader in NODES and leader!=self.name,'No current upstream leader')
            require(local['sender']==NODES[leader],'Standby is not streaming from current leader')
            require(not slot['active'] and usable(slot,False),'Unexpected demoted slot; do not drop')
            upstream=self.state(leader)
            require(not upstream['recovery'] and usable(upstream['slot'],False),'Upstream slot is not safe')
            require(lsn(upstream['slot']['confirmed'])>=lsn(slot['confirmed']),'Upstream has not acknowledged old slot position')
            # Recheck DCS policy; local flock serializes this with pre_promote.
            latest=self.dcs.snapshot()
            require(latest['leader']==snapshot['leader'] and latest['cdc_guard']==snapshot['cdc_guard'],'Policy changed during repair')
            dropped=self.query("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name=%s AND pg_is_in_recovery() AND NOT active AND NOT synced AND failover AND NOT temporary AND database='crawler' AND plugin='pgoutput' AND invalidation_reason IS NULL AND wal_status IN ('reserved','extended') AND confirmed_flush_lsn=%s::pg_lsn",(SLOT,slot['confirmed']))
            if not dropped:return {'state':'RECHECK_CHANGED_STANDBY_SLOT'}
            emit('demoted_local_slot_removed',node=self.name,upstream=leader,confirmed=slot['confirmed'])
            return {'state':'WAITING_FOR_NATIVE_SLOT_SYNC'}
        return {'state':'STANDBY_READY' if usable(slot) else 'WAITING_FOR_NATIVE_SLOT_SYNC','leader':leader}

    def pre_promote(self):
        snapshot=self.dcs.snapshot();policy=snapshot['policy'];local=self.state()
        require(snapshot['leader']['value']==self.name,'Patroni must acquire leader key before CDC check')
        require(policy and policy.get('version')==1 and self.name in policy['allowed'],'Candidate excluded by durable CDC policy')
        require(policy['system_id']==snapshot['initialize']['value']==str(self.query('SELECT system_identifier FROM pg_control_system()')[0][0]),'System identifier mismatch')
        require(local['recovery'] and usable(local['slot']),'Candidate lacks a permanent valid synchronized CDC slot')
        require(lsn(local['replay'])>=lsn(policy['floor']),'Candidate has not replayed admission WAL')
        # Reset inherited per-node settings before becoming writable. Until the
        # new leader publishes its own policy it must wait for BOTH other peers.
        self.set_barrier(set(NODES)-{self.name})
        latest=self.dcs.snapshot()
        require(latest['leader']==snapshot['leader'] and latest['cdc_guard']==snapshot['cdc_guard'],'Promotion fence changed during check')
        emit('promotion_allowed',node=self.name,generation=policy['generation'])

    def once(self):
        snapshot=self.dcs.snapshot();local=self.state()
        result=self.standby(snapshot,local) if local['recovery'] else self.primary(snapshot,local)
        return {'checked_at':time.time(),'node':self.name,**result}

def main():
    global SLOT
    p=argparse.ArgumentParser();p.add_argument('--pre-promote',action='store_true');p.add_argument('--once',action='store_true');p.add_argument('--repair-probe-slot');args=p.parse_args()
    if args.repair_probe_slot:
        require(args.once and not args.pre_promote and re.fullmatch(r'crawl_cdc_repair_probe_[a-f0-9]{8}',args.repair_probe_slot),'Only isolated one-shot standby repair probes are accepted')
        SLOT=args.repair_probe_slot
    os.umask(0o077);guard=Guard()
    with (STATE/'lock').open('a') as lock:
        if args.pre_promote:
            # Bound hook duration; failures cancel promotion instead of bypassing it.
            signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(RuntimeError('Promotion check timeout')))
            signal.alarm(12)
            try:
                fcntl.flock(lock,fcntl.LOCK_EX);guard.pre_promote();return
            finally:signal.alarm(0)
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if args.repair_probe_slot:
                    snapshot=guard.dcs.snapshot();local=guard.state()
                    require(local['recovery'],'Probe repair must run on a standby')
                    result=guard.standby(snapshot,local)
                else:result=guard.once()
            except Exception as error:
                # Never print configuration/credentials or libpq exception text.
                result={'checked_at':time.time(),'node':guard.name,'state':'ATTENTION','error_type':type(error).__name__}
                emit('reconcile_failed',error_type=type(error).__name__,reason=str(error) if type(error) is RuntimeError else 'Database or transport operation failed')
                if args.once:raise
            finally:fcntl.flock(lock,fcntl.LOCK_UN)
            if not args.repair_probe_slot:
                tmp=STATE/'status.json.tmp';tmp.write_text(json.dumps(result));tmp.replace(STATE/'status.json')
            if args.once:print(json.dumps(result));return
            time.sleep(2)

if __name__=='__main__':
    try:main()
    except Exception as error:
        emit('guard_failed',error_type=type(error).__name__,reason=str(error) if type(error) is RuntimeError else 'Database or transport operation failed');raise SystemExit(1)
