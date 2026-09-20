#!/usr/bin/env python3
"""Bounded switchover or leader/DCS network partition; restores only its own rules."""
import argparse
import base64
import concurrent.futures
import json
from pathlib import Path
import runpy
import shlex
import ssl
import subprocess
import time
import urllib.request
import uuid

m = runpy.run_path(str(Path(__file__).with_name('bootstrap-coordination.py')))
run, put, NODES, ROOT, PRIVATE = (m[k] for k in ('run','put','NODES','ROOT','PRIVATE'))
p=argparse.ArgumentParser();p.add_argument('mode',choices=['switchover','dcs-partition']);a=p.parse_args()
ctx=ssl.create_default_context(cafile=str(PRIVATE/'rest-pki/ca.crt'))
ctx.load_cert_chain(str(PRIVATE/'rest-pki/client.crt'),str(PRIVATE/'rest-pki/client.key'))

def api(ip,path,body=None):
    headers={}
    if body is not None:
        headers['Authorization']='Basic '+base64.b64encode(('operator:'+(PRIVATE/'rest-password').read_text().strip()).encode()).decode()
        headers['Content-Type']='application/json'
    req=urllib.request.Request('https://'+ip+':8008'+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req,context=ctx,timeout=10) as f: return json.load(f) if body is None else f.read().decode()

def sql(ip,statement):
    return run(ip,'sudo -n -u postgres psql -XAt -v ON_ERROR_STOP=1 -d crawler_validation_ingestor',statement,capture=True).stdout.strip()

def states():
    with concurrent.futures.ThreadPoolExecutor() as ex:
        return dict(ex.map(lambda pair:(pair[0],api(pair[1],'/patroni')),NODES.items()))

initial=states(); leaders=[n for n,d in initial.items() if d['role']=='primary'];assert len(leaders)==1
leader=leaders[0]; leader_ip=NODES[leader]
cluster=api(leader_ip,'/cluster')
candidates=[member['name'] for member in cluster['members'] if member['role']=='sync_standby']
assert candidates, 'Require healthy synchronous candidate'
candidate=candidates[0]
tag=uuid.uuid4().hex[:16];schema='ha_verify_'+tag
sql(leader_ip,f'CREATE SCHEMA {schema}; CREATE TABLE {schema}.probe(seq int PRIMARY KEY); GRANT USAGE ON SCHEMA {schema} TO crawler_ingestor_validation; GRANT SELECT,INSERT ON {schema}.probe TO crawler_ingestor_validation;')
probe=None;rollback=None; logpath=ROOT/f'ops/checks/2026-09-20-pg-{a.mode}.raw.txt'
evidence={'mode':a.mode,'oldLeader':leader,'candidate':candidate,'schema':schema}
try:
    with open(logpath,'w') as log:
        probe=subprocess.Popen(['kubectl','-n','crawl-validation','exec','-i','pg-ha-probe','--','node','--input-type=module','-',schema],stdin=subprocess.PIPE,stdout=log,stderr=log,text=True)
        probe.stdin.write((ROOT/'ops/postgresql-ha/write-probe.mjs').read_text());probe.stdin.close()
        time.sleep(5)
        assert probe.poll() is None, 'Write probe exited before fault injection'
        fault_start=time.monotonic();evidence['faultAt']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        if a.mode=='switchover':
            evidence['response']=api(leader_ip,'/switchover',{'leader':leader,'candidate':candidate})
        else:
            # Dedicated chain touches only this PG leader's DCS egress, not SSH/PG/Kafka.
            chain='CRAWL_HA_'+tag[:8].upper()
            rollback=f'/run/crawl-ha-rollback-{tag}.sh'
            undo=f'#!/bin/sh\niptables -D OUTPUT -j {chain} 2>/dev/null || true\niptables -F {chain} 2>/dev/null || true\niptables -X {chain} 2>/dev/null || true\n'
            put(leader_ip,rollback,undo,mode=0o700)
            command=f'set -eu; sudo -n systemd-run --quiet --unit=crawl-ha-rollback-{tag} --on-active=90s /bin/sh {rollback}; sudo -n iptables -N {chain}; '
            command+='; '.join(f'sudo -n iptables -A {chain} -p tcp -d {ip} --dport 2379 -j REJECT' for ip in NODES.values())
            command+=f'; sudo -n iptables -I OUTPUT 1 -j {chain}'
            run(leader_ip,command,capture=True)
        observed=[];new_leader=None
        for _ in range(55):
            current=states()
            writable=[n for n,d in current.items() if d['role']=='primary' and d['state']=='running']
            assert len(writable)<=1, 'More than one reported writable primary'
            observed.append({'elapsed':round(time.monotonic()-fault_start,2),'primary':writable})
            if writable and writable[0]!=leader:
                new_leader=writable[0];break
            time.sleep(1)
        assert new_leader, 'No new primary elected within deadline'
        evidence['newLeader']=new_leader;evidence['electionObservedSeconds']=round(time.monotonic()-fault_start,2)
        evidence['roleObservations']=observed
        # Before healing the network, the old primary must be stopped or read-only.
        try:
            old_state = sql(leader_ip, 'SELECT pg_is_in_recovery();')
            assert old_state == 't', 'Old primary remains writable after promotion'
            evidence['oldPrimaryFenced'] = 'read-only standby'
        except subprocess.CalledProcessError as error:
            assert 'connection' in (error.stderr or '').lower(), 'Unexpected SQL verification failure'
            evidence['oldPrimaryFenced'] = 'PostgreSQL unavailable'
        if rollback:
            run(leader_ip,'sudo -n /bin/sh '+rollback,capture=True)
        probe.wait(timeout=90)
        assert probe.returncode==0, 'Write verification failed; inspect protected local evidence'
    lines=logpath.read_text().splitlines()
    result=next(json.loads(line) for line in lines if line.startswith('{') and json.loads(line).get('phase')=='passed')
    evidence['writeProbe']=result
    # Allow replica apply to catch up; all acknowledgements must exist on every member.
    for _ in range(20):
        try:
            counts={n:int(sql(ip,f'SELECT count(*) FROM {schema}.probe WHERE seq<={result["acknowledged"]};')) for n,ip in NODES.items()}
            if all(v==result['acknowledged'] for v in counts.values()):break
        except (subprocess.CalledProcessError,ValueError):pass
        time.sleep(1)
    else:raise RuntimeError('Replicas failed acknowledgement verification')
    evidence['replicaCounts']=counts;evidence['status']='PASSED'
    target=ROOT/f'ops/checks/2026-09-20-pg-{a.mode}.json'
    target.write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence),flush=True)
finally:
    if rollback:
        run(leader_ip,'sudo -n /bin/sh '+rollback,capture=True)
    if probe and probe.poll() is None:
        probe.terminate();probe.wait(timeout=10)
    current=states();primaries=[n for n,d in current.items() if d['role']=='primary' and d['state']=='running']
    if len(primaries)==1:sql(NODES[primaries[0]],f'DROP SCHEMA {schema} CASCADE;')
