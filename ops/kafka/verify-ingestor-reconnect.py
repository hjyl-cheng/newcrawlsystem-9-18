#!/usr/bin/env python3
"""Stop only the results group's coordinator broker and verify live ingestor recovery.

Uses the 9094 mTLS transport. Four-minute host rescue remains armed until ISR3.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import struct
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
m = runpy.run_path(str(Path(__file__).with_name('secure-listener.py')))
sql = runpy.run_path(str(ROOT/'ops/postgresql-ha/verify-application-sql.py'))


def pods():
    values = json.loads(subprocess.check_output(['kubectl','-n','crawl-validation','get','pods','-l','app=data-ingestor','-o','json'],text=True))['items']
    result = {}
    for pod in values:
        status = pod['status']['containerStatuses'][0]
        result[pod['metadata']['uid']] = {'name':pod['metadata']['name'], 'restarts':status['restartCount'],
            'ready':status['ready'], 'image':status['imageID']}
    assert len(result)==2
    return result


def coordinator():
    group = b'crawler-results-apply-validation-v1'
    request = struct.pack('!hhihh',10,0,419,-1,len(group))+group
    def receive(sock,n):
        data=b''
        while len(data)<n:
            part=sock.recv(n-len(data))
            if not part:raise RuntimeError('Incomplete coordinator response')
            data+=part
        return data
    context=ssl.create_default_context(cafile=str(m['PRIVATE']/'ca.crt'))
    context.load_cert_chain(str(m['PRIVATE']/'operator.crt'),str(m['PRIVATE']/'operator.key'))
    with socket.create_connection(('10.4.4.2',9094),timeout=5) as raw:
        with context.wrap_socket(raw,server_hostname='10.4.4.2') as sock:
            sock.sendall(struct.pack('!i',len(request))+request)
            size=struct.unpack('!i',receive(sock,4))[0];assert 16<=size<10000
            reply=receive(sock,size)
    correlation,error,node,length=struct.unpack('!ihih',reply[:12])
    assert correlation==419 and error==0 and node in [1,2,3]
    host=reply[12:12+length].decode();port=struct.unpack('!i',reply[12+length:16+length])[0]
    assert host==m['NODES']['s'+str(node)] and port==9094
    return 's'+str(node)


def same_processes(before):
    after=pods();assert set(before)==set(after),'Unexpected Pod replacement'
    assert all(after[k]['restarts']==before[k]['restarts'] for k in before),'Ingestor container restarted'
    return after


def ingest():
    owner=runpy.run_path(str(ROOT/'ops/postgresql-ha/prepare-patroni.py'))['envfile'](ROOT/'secrets/validation-storage.env')
    leader=sql['pg']['leader']()
    env=dict(os.environ,PGSSLMODE='verify-full',NODE_EXTRA_CA_CERTS=str(ROOT/'secrets/postgresql-ha/sql-pki/ca.crt'),
        PGHOST=leader,PGPORT='5432',PGUSER='crawler',PGPASSWORD=owner['CRAWLER_PASSWORD'],
        PGDATABASE='crawler_validation_ingestor',CONSUMER_PGHOST=leader,CONSUMER_PGPORT='6432')
    with sql['forward']('data-ingestor',18081,8080):
        return sql['node_check']('ops/ingestor/verify-validation.mjs',env)


def main():
    if sys.argv[1:]!=['--execute']:raise SystemExit('Requires --execute; stops one broker service temporarily')
    evidence={'startedAt':datetime.now(timezone.utc).isoformat(),'status':'RUNNING','transport':'9094 mTLS client path; broker/controller traffic still legacy'}
    target=None;armed=False
    unit='crawl-kafka-ingestor-rescue-'+uuid.uuid4().hex
    try:
        evidence['beforeCluster']=m['stable']('s1')
        before=pods();assert all(p['ready'] for p in before.values())
        evidence['beforePods']=before
        target=coordinator();evidence['target']=target
        m['run'](target,'sudo systemd-run --unit='+unit+' --on-active=4m /usr/bin/systemctl start kafka')
        armed=True
        m['run'](target,'sudo systemctl stop kafka')
        print(target+': result-group coordinator stopped; checking bounded recovery for up to 90 seconds',flush=True)
        # Several dependency polling cycles must execute while the old coordinator is down.
        started=time.monotonic();evidence['degradedReadiness']=[]
        for _ in range(18):
            time.sleep(5);sample=same_processes(before)
            elapsed=round(time.monotonic()-started,2)
            ready=all(p['ready'] for p in sample.values())
            evidence['degradedReadiness'].append({'seconds':elapsed,'allReady':ready})
            if elapsed>=30 and ready:break
        else:raise RuntimeError('Ingestor readiness did not recover within 90 seconds')
        assert m['run'](target,'systemctl show kafka --property=ActiveState --value').stdout.strip()=='inactive'
        assert all(p['ready'] for p in pods().values())
        evidence['degradedIngestor']=ingest()
        evidence['degradedPods']=same_processes(before)
        print('One broker down: both ingestors stayed alive; signed/duplicate/invalid records handled',flush=True)
        m['run'](target,'sudo systemctl start kafka')
        evidence['recoveredCluster']=m['stable'](target)
        for _ in range(4):
            time.sleep(5);same_processes(before)
        evidence['recoveredIngestor']=ingest()
        evidence['recoveredPods']=same_processes(before)
        assert all(p['ready'] for p in evidence['recoveredPods'].values())
        evidence['status']='PASSED'
        print('Broker restored: ISR3, both ingestors unchanged, post-recovery ingestion passed',flush=True)
    except BaseException as error:
        evidence['status']='FAILED';evidence['error']=str(error);raise
    finally:
        try:
            if armed:
                # Preserve rescue if recovery/check itself fails.
                m['run'](target,'sudo systemctl start kafka')
                m['stable'](target)
                m['run'](target,'sudo systemctl stop '+unit+'.timer')
                evidence['rescue']='Kafka restored; ISR3/quorum healthy; rescue timer cancelled'
        except BaseException as error:
            evidence['status']='FAILED';evidence['rescueError']=str(error);raise
        finally:
            evidence['finishedAt']=datetime.now(timezone.utc).isoformat()
            (ROOT/'ops/checks/2026-09-21-ingestor-kafka-tls-reconnect.json').write_text(json.dumps(evidence,indent=2)+'\n')


if __name__=='__main__':main()
