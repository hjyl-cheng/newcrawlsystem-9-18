#!/usr/bin/env python3
"""Admin requests from a labelled Pod; REST is never exposed publicly."""
import json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
POD='cdc-admin-probe'
FETCH="let s='';for await(const c of process.stdin)s+=c;let x=JSON.parse(s);let r=await fetch('http://kafka-connect.crawl-validation.svc:8083'+x.path,{method:x.method,headers:{'Content-Type':'application/json'},body:x.body===null?undefined:JSON.stringify(x.body),signal:AbortSignal.timeout(30000)});console.log(JSON.stringify({status:r.status,text:await r.text()}));"
def kube(*args,**kw):return subprocess.run(['kubectl',*args],check=True,capture_output=True,text=True,timeout=90,**kw)
def api(path,body=None,method=None):
 raw=kube('-n','crawl-validation','exec','-i',POD,'--','node','--input-type=module','-e',FETCH,input=json.dumps({'path':path,'body':body,'method':method or ('PUT' if body is not None else 'GET')}))
 r=json.loads(raw.stdout)
 if not 200<=r['status']<300:raise RuntimeError('Connect REST '+str(r['status'])+': '+r['text'][:1200])
 return json.loads(r['text']) if r['text'] else None

def create_probe():
 image=kube('-n','crawl-validation','get','deployment','runtime-smoke','-o','jsonpath={.spec.template.spec.containers[0].image}').stdout
 pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':POD,'namespace':'crawl-validation','labels':{'cdc-admin':'true'}},'spec':{'automountServiceAccountToken':False,'activeDeadlineSeconds':3600,'restartPolicy':'Never','nodeSelector':{'kubernetes.io/hostname':'a3'},'tolerations':[{'key':'node-role.kubernetes.io/control-plane','operator':'Exists','effect':'NoSchedule'}],'containers':[{'name':'admin','image':image,'command':['node','-e','setInterval(()=>{},1000)'],'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'250m','memory':'128Mi'}}}]}}
 kube('apply','-f','-',input=json.dumps(pod));kube('-n','crawl-validation','wait','--for=condition=Ready','pod/'+POD,'--timeout=90s')

if __name__=='__main__':
 create_probe()
 spec=json.loads((ROOT/'ops/cdc/connector.json').read_text())
 validation=api('/connector-plugins/io.debezium.connector.postgresql.PostgresConnector/config/validate',spec['config'])
 errors=[{'name':x['definition']['name'],'errors':x['value'].get('errors')} for x in validation['configs'] if x['value'].get('errors')]
 assert validation['error_count']==0,errors
 api('/connectors/'+spec['name']+'/config',spec['config'])
 for _ in range(90):
  status=api('/connectors/'+spec['name']+'/status')
  if status['connector']['state']=='RUNNING' and status['tasks'] and all(t['state']=='RUNNING' for t in status['tasks']):break
  if any(t['state']=='FAILED' for t in status['tasks']):raise RuntimeError(json.dumps(status))
  time.sleep(1)
 else:raise RuntimeError('Connector not running')
 print(json.dumps({'status':'PASSED','connect':api('/'),'connector':status},indent=2))
