#!/usr/bin/env python3
"""Bounded signed S3 tests through the real Kubernetes Service; infrastructure only."""
import argparse,base64,hashlib,json,runpy,ssl,subprocess,time,urllib.request,uuid
from pathlib import Path
import boto3
from botocore.config import Config
ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run=h['run']
NODES={'s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
POD='seaweed-service-probe-'+uuid.uuid4().hex[:8]
BUCKET='crawl-validation-objects'
ENDPOINT='http://seaweed-s3.crawl-validation.svc:8333'
NODE_FETCH="""let s='';for await(const c of process.stdin)s+=c;const {requests}=JSON.parse(s);const out=[];for(const r of requests){const response=await fetch(r.url,{method:r.method,body:r.body?Buffer.from(r.body,'base64'):undefined,headers:{Connection:'close'},signal:AbortSignal.timeout(15000)});const data=Buffer.from(await response.arrayBuffer());if(!response.ok)throw Error('S3 status '+response.status);out.push({status:response.status,sha256:(await import('node:crypto')).createHash('sha256').update(data).digest('hex'),body:r.list?data.toString():undefined});}console.log(JSON.stringify(out));"""
def kubectl(*args,**kwargs):return subprocess.run(['kubectl',*args],check=True,capture_output=True,text=True,timeout=90,**kwargs)
def probe(client,phase):
    key='infra/service-'+phase+'-'+uuid.uuid4().hex+'.bin'
    data=('service-'+phase+'\n').encode()*128
    requests=[]
    for operation,method in [('put_object','PUT'),('get_object','GET'),('list_objects_v2','GET')]:
        params={'Bucket':BUCKET,'Prefix':key} if operation.startswith('list') else {'Bucket':BUCKET,'Key':key}
        requests.append({'url':client.generate_presigned_url(operation,Params=params,ExpiresIn=90,HttpMethod=method),'method':method,'body':base64.b64encode(data).decode() if method=='PUT' else None,'list':operation.startswith('list')})
    # Signed URLs travel on stdin, never shell arguments/logs or a Kubernetes Secret.
    raw=kubectl('-n','crawl-validation','exec','-i',POD,'--','node','--input-type=module','-e',NODE_FETCH,input=json.dumps({'requests':requests}))
    result=json.loads(raw.stdout)
    assert result[1]['sha256']==hashlib.sha256(data).hexdigest()
    assert key in result[2]['body']
    return {'phase':phase,'key':key,'bytes':len(data),'sha256':result[1]['sha256'],'putGetList':'PASSED'}
def pg_switchover(client,evidence):
    private=ROOT/'secrets/postgresql-ha'
    ctx=ssl.create_default_context(cafile=str(private/'rest-pki/ca.crt'))
    ctx.load_cert_chain(str(private/'rest-pki/client.crt'),str(private/'rest-pki/client.key'))
    def api(ip,path,body=None,method=None):
        headers={}
        if body is not None:
            headers={'Content-Type':'application/json','Authorization':'Basic '+base64.b64encode(('operator:'+(private/'rest-password').read_text().strip()).encode()).decode()}
        request=urllib.request.Request('https://'+ip+':8008'+path,method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(request,context=ctx,timeout=15) as response:return json.load(response) if body is None else response.read().decode()
    def cluster():return api(NODES['s1'],'/cluster')['members']
    original=next(m['name'] for m in cluster() if m['role']=='leader')
    candidate=next(m['name'] for m in cluster() if m['role']=='sync_standby')
    evidence['pg']={'original':original,'candidate':candidate}
    try:
        start=time.monotonic()
        evidence['pg']['response']=api(NODES[original],'/switchover',{'leader':original,'candidate':candidate})
        assert next(m['name'] for m in cluster() if m['role']=='leader')==candidate
        failures=[]
        for _ in range(30):
            try:evidence['probes'].append(probe(client,'pg-switched'));break
            except subprocess.CalledProcessError as error:
                failures.append(type(error).__name__);time.sleep(1)
        else:raise RuntimeError('Service failed to recover after PG switch')
        evidence['pg']['firstSuccessfulRoundtripSeconds']=round(time.monotonic()-start,2)
        evidence['pg']['failedProbeAttempts']=len(failures)
    finally:
        # Patroni may choose the third node as its synchronous standby. During
        # cleanup only, temporarily require both replicas so the original can
        # safely become the switchover candidate. Restore the original setting.
        original_count=api(NODES['s1'],'/config').get('synchronous_node_count',1)
        try:
            api(NODES['s1'],'/config',{'synchronous_node_count':2},'PATCH')
            for _ in range(60):
                members=cluster();leader=next(m['name'] for m in members if m['role']=='leader')
                if leader==original:break
                if any(m['name']==original and m['role']=='sync_standby' for m in members):
                    api(NODES[leader],'/switchover',{'leader':leader,'candidate':original});break
                time.sleep(1)
            assert next(m['name'] for m in cluster() if m['role']=='leader')==original,'Original primary not restored'
        finally:
            api(NODES['s1'],'/config',{'synchronous_node_count':original_count},'PATCH')
        evidence['pg']['cleanup']='original primary and synchronous_node_count restored; temporarily required two synchronous replicas for return'
    time.sleep(5)
    evidence['probes'].append(probe(client,'pg-original-restored'))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',required=True,action='store_true');parser.add_argument('--pg-switchover',action='store_true');args=parser.parse_args()
    keys=json.loads((ROOT/'secrets/seaweedfs/s3-credentials.json').read_text())['worker']
    client=boto3.client('s3',endpoint_url=ENDPOINT,aws_access_key_id=keys['accessKey'],aws_secret_access_key=keys['secretKey'],region_name='us-east-1',config=Config(signature_version='s3v4',s3={'addressing_style':'path'}))
    image=kubectl('-n','crawl-validation','get','deployment','runtime-smoke','-o','jsonpath={.spec.template.spec.containers[0].image}').stdout
    pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':POD,'namespace':'crawl-validation','labels':{'object-store-client':'true'}},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','activeDeadlineSeconds':900,'nodeSelector':{'kubernetes.io/hostname':'a2'},'tolerations':[{'key':'node-role.kubernetes.io/control-plane','operator':'Exists','effect':'NoSchedule'}],'containers':[{'name':'probe','image':image,'command':['node','-e','setInterval(()=>{},1000)'],'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}}}]}}
    evidence={'endpoint':ENDPOINT,'status':'RUNNING','probes':[]}
    unit='crawl-sw-failure-recover-'+uuid.uuid4().hex[:12]
    armed=False
    try:
        kubectl('create','-f','-',input=json.dumps(pod));kubectl('-n','crawl-validation','wait','--for=condition=Ready','pod/'+POD,'--timeout=90s')
        evidence['probes'].append(probe(client,'healthy'))
        if args.pg_switchover:
            pg_switchover(client,evidence)
        else:
            run(NODES['s1'],f'sudo systemd-run --unit={unit} --on-active=180s /usr/bin/systemctl start seaweedfs-filer seaweedfs-s3',capture=True);armed=True
            run(NODES['s1'],'sudo systemctl stop seaweedfs-s3',capture=True)
            # Filer has remote long-lived subscriptions. Deliberately simulate an
            # abrupt process failure, with stop pending to prevent immediate restart.
            run(NODES['s1'],'sudo systemctl stop --no-block seaweedfs-filer',capture=True)
            time.sleep(2)
            state=run(NODES['s1'],'systemctl show seaweedfs-filer -p ActiveState --value',capture=True).stdout.strip()
            if state not in ['inactive','failed']:
                try:
                    run(NODES['s1'],'sudo systemctl kill --signal=SIGKILL --kill-whom=main seaweedfs-filer',capture=True)
                except subprocess.CalledProcessError as error:
                    print('Stop diagnostic: '+(error.stderr or '').strip(),flush=True)
                    state=run(NODES['s1'],'systemctl show seaweedfs-filer -p ActiveState --value',capture=True).stdout.strip()
                    if state not in ['inactive','failed']:raise
            evidence['filerStop']='graceful' if state=='inactive' else 'forced after stop request'
            time.sleep(8)
            state=run(NODES['s1'],'systemctl show seaweedfs-filer seaweedfs-s3 -p ActiveState --value',capture=True).stdout.splitlines()
            assert all(x in ['inactive','failed'] for x in state if x),state
            evidence['stoppedBackend']='s1 Filer + S3 Gateway services; not whole-host failure'
            evidence['probes'].append(probe(client,'s1-down'))
            run(NODES['s1'],'sudo systemctl start seaweedfs-filer seaweedfs-s3',capture=True)
            time.sleep(8)
            evidence['probes'].append(probe(client,'s1-restored'))
            # Delete one real entry Pod; fresh requests use ClusterIP, no port-forward.
            entries=json.loads(kubectl('-n','crawl-validation','get','pods','-l','app=seaweed-s3-entry','-o','json').stdout)['items']
            assert len(entries)==2
            evidence['entryNodes']=sorted(p['spec']['nodeName'] for p in entries)
            kubectl('-n','crawl-validation','delete','pod',entries[0]['metadata']['name'],'--wait=false')
            time.sleep(3)
            evidence['probes'].append(probe(client,'entry-replacement'))
            kubectl('-n','crawl-validation','rollout','status','deployment/seaweed-s3-entry','--timeout=90s')
        evidence['status']='PASSED'
    finally:
        if armed:
            run(NODES['s1'],'sudo systemctl start seaweedfs-filer seaweedfs-s3',capture=True)
            run(NODES['s1'],f'sudo systemctl stop {unit}.timer',capture=True)
        kubectl('-n','crawl-validation','delete','pod',POD,'--ignore-not-found','--wait=false')
    (ROOT/('ops/checks/2026-09-21-seaweed-pg-switchover.json' if args.pg_switchover else 'ops/checks/2026-09-21-seaweed-service-failover.json')).write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence))
if __name__=='__main__':main()
