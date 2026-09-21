#!/usr/bin/env python3
"""Bounded standby outages, DCS partition and isolated demoted-slot repair."""
import argparse,json,shlex,subprocess,time,uuid
import common as c

def members(exclude=()):
    for name in c.NODES:
        if name in exclude:continue
        try:return c.api(name,'/cluster',timeout=3)['members']
        except Exception:continue
    raise RuntimeError('No available Patroni cluster view')

def leader(exclude=()):return next(m['name'] for m in members(exclude) if m['role']=='leader')

def wait_for(check,seconds=120):
    deadline=time.monotonic()+seconds;last=None
    while time.monotonic()<deadline:
        try:
            last=check()
            if last:return last
        except Exception as error:last=type(error).__name__
        time.sleep(1)
    raise RuntimeError('Deadline exceeded: '+str(last))

def guard_state(node):return json.loads(c.run(c.NODES[node],'sudo cat /var/lib/crawl-cdc-guard/status.json',capture=True).stdout)

def ready(primary,names):
    state=guard_state(primary)
    return state if state['state']=='READY' and set(state['candidates'])==set(names) and time.time()-state['checked_at']<10 else None

def slot_state(node,slot):
    raw=c.sql(node,"SELECT json_build_object('recovery',pg_is_in_recovery(),'synced',synced,'temporary',temporary,'invalid',invalidation_reason,'active',active,'confirmed',confirmed_flush_lsn) FROM pg_replication_slots WHERE slot_name='"+slot+"';")
    return json.loads(raw) if raw else None

def tag(node,value):
    path='/etc/crawl-patroni/patroni.yml';cfg=json.loads(c.run(c.NODES[node],'sudo cat '+path,capture=True).stdout)
    old=cfg.setdefault('tags',{}).get('nofailover',False);cfg['tags']['nofailover']=value
    c.put(c.NODES[node],path,json.dumps(cfg,indent=2),'postgres');c.run(c.NODES[node],'sudo systemctl reload crawl-patroni',capture=True)
    return old

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
    run_id=uuid.uuid4().hex;short=run_id[:8];probe_slot='crawl_cdc_repair_probe_'+short
    raw=c.ROOT/('ops/checks/cdc-guard-'+run_id+'.raw.txt')
    evidence={'runId':run_id,'status':'RUNNING','phases':[],'faults':[]}
    consumer=None;stopped=None;rollback=None;old_tag=None;original=leader();expected=[];probe_created=False
    def events():
        rows=[]
        for line in raw.read_text().splitlines():
            try:row=json.loads(line)
            except json.JSONDecodeError:continue
            if row.get('phase')=='event':rows.append(row)
        return rows
    def insert(phase,exclude=()):
        rows=[]
        for _ in range(4):
            identifier=str(uuid.uuid4());channel='channel-'+str(len(expected)%2);expected.append(identifier)
            payload=json.dumps({'event_id':identifier,'run_id':run_id,'seq':len(expected),'phase':phase,'channel_id':channel})
            rows.append("('"+identifier+"','infra','"+channel+"','GuardProbe','"+payload+"'::jsonb)")
        statement="SET statement_timeout='15s'; INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES "+','.join(rows)+" ON CONFLICT(id) DO NOTHING;"
        wait_for(lambda:write(statement,exclude),60)
        wait_for(lambda:set(expected).issubset({row['id'] for row in events()}),100)
        evidence['phases'].append({'phase':phase,'acknowledged':len(expected),'uniqueReceived':len({r['id'] for r in events()}),'primary':leader(exclude)})
        print(phase+': '+str(len(expected))+' confirmed events received',flush=True)
    def write(statement,exclude):
        c.sql(leader(exclude),statement,'crawler');return True
    def pump():
        primary=leader();identifier=str(uuid.uuid4())
        c.sql(primary,'CHECKPOINT;')
        payload=json.dumps({'event_id':identifier,'run_id':'repair-probe-'+short,'channel_id':'repair-probe'})
        c.sql(primary,"SET statement_timeout='5s'; INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES ('"+identifier+"','infra','repair-probe','RepairProbe','"+payload+"'::jsonb); SELECT count(*) FROM pg_logical_slot_get_binary_changes('"+probe_slot+"',NULL,NULL,'proto_version','1','publication_names','crawl_cdc_validation');",'crawler')
    def probe_ready():
        primary=leader();states={n:slot_state(n,probe_slot) for n in c.NODES}
        return states if all(s and not s['temporary'] and not s['invalid'] and (n==primary or s['synced']) for n,s in states.items()) else None
    try:
        with raw.open('w') as log:
            consumer=subprocess.Popen(['node',str(c.ROOT/'ops/cdc/consume-probe.mjs'),run_id,'32','1500000'],stdout=log,stderr=log)
            wait_for(lambda:ready(original,set(c.NODES)-{original}))
            insert('healthy')
            for sync in [False,True]:
                current=leader();target=next(m['name'] for m in members() if m['name']!=current and (m['role']=='sync_standby')==sync)
                stopped=target;unit='cdc-guard-restart-'+short+('-sync' if sync else '-async')
                c.run(c.NODES[target],'sudo systemd-run --quiet --unit='+unit+' --on-active=100s /bin/systemctl start crawl-patroni',capture=True)
                start=time.monotonic();c.run(c.NODES[target],'sudo systemctl stop crawl-patroni',capture=True)
                remaining=set(c.NODES)-{current,target}
                wait_for(lambda:ready(current,remaining),70)
                insert(('sync' if sync else 'async')+'-standby-offline',exclude=(target,))
                evidence['faults'].append({'kind':'synchronous-standby-service-stop' if sync else 'asynchronous-standby-service-stop','node':target,'observedRecoverySeconds':round(time.monotonic()-start,2),'barrier':c.sql(current,'SHOW synchronized_standby_slots;'),'guard':guard_state(current)})
                c.run(c.NODES[target],'sudo systemctl start crawl-patroni',capture=True);stopped=None
                c.run(c.NODES[target],'sudo systemctl stop '+unit+'.timer',capture=True)
                wait_for(lambda:ready(current,set(c.NODES)-{current}),150)
                insert(('sync' if sync else 'async')+'-standby-returned')

            # Extra slot is newly created on the primary, therefore synced=false.
            # It reproduces the first-demotion issue without altering the active
            # Debezium slot, its offset, or any Kafka topic configuration.
            c.sql(original,"SET statement_timeout='10s'; SELECT slot_name FROM pg_create_logical_replication_slot('"+probe_slot+"','pgoutput',false,false,true);",'crawler');probe_created=True
            for attempt in range(90):
                if attempt%10==0:pump()
                initial_slots=probe_ready()
                if initial_slots:break
                time.sleep(2)
            else:raise RuntimeError('Repair probe never synchronized')
            evidence['repairProbeBefore']=initial_slots
            old_tag=tag(original,True)
            wait_for(lambda:next(m for m in members() if m['name']==original).get('tags',{}).get('nofailover'))
            chain='CDC_GUARD_'+short.upper();rollback='/run/cdc-guard-rollback-'+short+'.sh'
            c.put(c.NODES[original],rollback,'#!/bin/sh\niptables -D OUTPUT -j '+chain+' 2>/dev/null || true\niptables -F '+chain+' 2>/dev/null || true\niptables -X '+chain+' 2>/dev/null || true\n',mode=0o700)
            command='set -eu; sudo systemd-run --quiet --unit=cdc-guard-rollback-'+short+' --on-active=100s /bin/sh '+rollback+'; sudo iptables -N '+chain+'; '
            command+='; '.join('sudo iptables -A '+chain+' -p tcp -d '+ip+' --dport 2379 -j REJECT' for ip in c.NODES.values())
            command+='; sudo iptables -I OUTPUT 1 -j '+chain
            start=time.monotonic();c.run(c.NODES[original],command,capture=True)
            def new_leader():
                name=leader((original,));return name if name!=original else None
            elected=wait_for(new_leader,65)
            wait_for(lambda:ready(elected,set(c.NODES)-{original,elected}),60)
            try:
                readonly=c.sql(original,'SELECT pg_is_in_recovery();');assert readonly=='t'
                fenced='read-only standby'
            except subprocess.CalledProcessError as error:
                assert 'connection' in (error.stderr or '').lower(), 'Unexpected SQL error while checking old-primary fencing'
                fenced='PostgreSQL unavailable'
            insert('primary-dcs-isolated',exclude=(original,))
            evidence['faults'].append({'kind':'leader-DCS-network-partition','from':original,'to':elected,'oldPrimaryFenced':fenced,'observedRecoverySeconds':round(time.monotonic()-start,2),'guard':guard_state(elected)})
            c.run(c.NODES[original],'sudo /bin/sh '+rollback,capture=True);rollback=None
            c.run(c.NODES[original],'sudo systemctl stop cdc-guard-rollback-'+short+'.timer',capture=True)
            wait_for(lambda:c.sql(original,'SELECT pg_is_in_recovery();')=='t',120)
            wait_for(lambda:next(m for m in members() if m['name']==original).get('tags',{}).get('nofailover'),30)
            stale=slot_state(original,probe_slot);assert stale and not stale['synced'] and not stale['active'],stale
            # Invoke the exact installed repair path on the isolated extra slot.
            output=c.run(c.NODES[original],'sudo -u postgres /usr/bin/python3 /opt/crawlsystem/cdc/ha-guard.py --once --repair-probe-slot '+probe_slot,capture=True).stdout
            evidence['repairProbe']={'node':original,'slot':probe_slot,'stale':stale,'automaticRepairOutput':[json.loads(line) for line in output.splitlines()]}
            for attempt in range(90):
                if attempt%10==0:pump()
                restored=probe_ready()
                if restored:break
                time.sleep(2)
            else:raise RuntimeError('Repaired probe slot did not synchronize')
            evidence['repairProbe']['after']=restored
            tag(original,old_tag);old_tag=None
            wait_for(lambda:ready(elected,set(c.NODES)-{elected}),150)
            insert('old-primary-rejoined')

            if leader()!='s1':
                count=c.api(elected,'/config')['synchronous_node_count']
                try:
                    c.api(elected,'/config',{'synchronous_node_count':2},'PATCH')
                    wait_for(lambda:any(m['name']=='s1' and m['role']=='sync_standby' for m in members()))
                    c.api(elected,'/switchover',{'leader':elected,'candidate':'s1'})
                    wait_for(lambda:leader()=='s1')
                finally:c.api('s1','/config',{'synchronous_node_count':count},'PATCH')
            wait_for(lambda:ready('s1',{'s2','s3'}),150)
            insert('preferred-primary-restored')
            consumer.wait(timeout=30);assert consumer.returncode==0
        messages=events();assert {r['id'] for r in messages}==set(expected)
        evidence.update(status='PASSED',acknowledged=len(expected),uniqueReceived=len({r['id'] for r in messages}),duplicates=len(messages)-len(expected),finalPrimary=leader(),events=messages)
        (c.ROOT/'ops/checks/2026-09-21-cdc-guard-failover.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps({k:evidence[k] for k in ['status','acknowledged','uniqueReceived','duplicates','finalPrimary']}))
    finally:
        if rollback:c.run(c.NODES[original],'sudo /bin/sh '+rollback,capture=True)
        if stopped:c.run(c.NODES[stopped],'sudo systemctl start crawl-patroni',capture=True)
        if old_tag is not None:tag(original,old_tag)
        if consumer and consumer.poll() is None:consumer.terminate();consumer.wait(timeout=15)
        if probe_created:
            # Only the unique test slot, never the real Debezium slot. Native
            # synchronization removes the corresponding standby copies.
            c.sql(leader(),"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name='"+probe_slot+"' AND NOT active;")
        if evidence['status']!='PASSED':
            evidence['status']='FAILED';(c.ROOT/('ops/checks/cdc-guard-'+run_id+'-failed.raw.txt')).write_text(json.dumps(evidence,indent=2)+'\n')

if __name__=='__main__':main()
