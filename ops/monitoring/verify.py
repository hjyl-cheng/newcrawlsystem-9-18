#!/usr/bin/env python3
"""Verify real scrapes, DB-backed UI, bounded alert outage and TSDB persistence.
Only an exporter and one Prometheus Pod are restarted; no data service is stopped.
"""
import argparse,base64,contextlib,importlib.util,json,subprocess,time,urllib.request,urllib.parse
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('nodes',ROOT/'ops/monitoring/bootstrap-nodes.py')
nodes=importlib.util.module_from_spec(spec);spec.loader.exec_module(nodes)


def kube(*args):return subprocess.check_output(['kubectl','-n','crawl-monitoring',*args],text=True)
def get(base,path,headers=None):
 with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers or {}),timeout=8) as r:return json.load(r)
def wait(check,timeout=180):
 end=time.monotonic()+timeout
 last=None
 while time.monotonic()<end:
  try:
   last=check()
   if last:return last
  except (OSError,ValueError,subprocess.CalledProcessError):pass
  time.sleep(3)
 raise RuntimeError('Verification condition did not become true before deadline')

@contextlib.contextmanager
def forward(pod,local,remote):
 process=subprocess.Popen(['kubectl','-n','crawl-monitoring','port-forward','pod/'+pod,str(local)+':'+str(remote)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 base='http://127.0.0.1:'+str(local)
 try:
  wait(lambda:get(base,'/api/v1/status/buildinfo') if remote==9090 else get(base,'/api/v2/status'),60)
  yield base
 finally:
  process.terminate()
  try:process.wait(timeout=5)
  except subprocess.TimeoutExpired:process.kill();process.wait()

def query(base,expr):return get(base,'/api/v1/query?'+urllib.parse.urlencode({'query':expr}))['data']['result']
def targets(base):return get(base,'/api/v1/targets')['data']['activeTargets']
def ready(base):
 t=targets(base)
 return len(t)==14 and all(x['health']=='up' for x in t)
def alerts(base):return [a for a in get(base,'/api/v2/alerts') if a['labels'].get('alertname')=='MonitoringTargetDown' and a['labels'].get('node')=='a3']


def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true',required=True);p.parse_args()
 evidence={'startedAt':datetime.now(timezone.utc).isoformat(),'phases':[]}
 def record(name,**data):
  evidence['phases'].append({'phase':name,**data});print(json.dumps(evidence['phases'][-1],ensure_ascii=False),flush=True)
 with forward('prometheus-1',28091,9090) as p1, forward('alertmanager-0',29093,9093) as am:
  with forward('prometheus-0',28090,9090) as p0:
   wait(lambda:ready(p0) and ready(p1))
   assert len(get(am,'/api/v2/status')['cluster']['peers'])==3
   assert not query(p0,'crawl_collector_success == 0')
   assert len(query(p0,'crawl_http_probe_success == 1'))==2
   assert len(query(p0,'kube_node_status_condition{condition="Ready",status="true"} == 1'))==3
   record('scraping',prometheusReplicas=2,targetsPerReplica=14,alertmanagerMembers=3)
   grafana='http://'+json.loads(kube('get','svc','grafana','-o','json'))['spec']['clusterIP']+':3000'
   auth={'Authorization':'Basic '+base64.b64encode(('admin:'+(ROOT/'secrets/monitoring/grafana-admin-password').read_text().strip()).encode()).decode()}
   for _ in range(6):assert get(grafana,'/api/health')['database']=='ok'
   try:get(grafana,'/api/search');raise AssertionError('Anonymous Grafana access allowed')
   except urllib.error.HTTPError as error:assert error.code==401
   settings=get(grafana,'/api/admin/settings',auth)
   assert settings['database']['type']=='postgres'
   assert settings['database']['name']=='crawler_grafana'
   dashboard=get(grafana,'/api/dashboards/uid/crawl-infrastructure',auth)
   assert len(dashboard['dashboard']['panels'])==16
   proxy=get(grafana,'/api/datasources/proxy/uid/prometheus/api/v1/query?query=up',auth)
   assert len(proxy['data']['result'])==14
   peers=get(grafana,'/api/datasources/proxy/uid/alertmanager/api/v2/status',auth)['cluster']['peers'];assert len(peers)==3
   record('grafana',database='postgres/crawler_grafana',anonymousStatus=401,dashboardPanels=16,datasourcesHealthy=True)
   # Automatic rescue remains available even if this verifier is interrupted.
   unit='crawl-monitoring-probe-rescue-'+str(int(time.time()))
   nodes.run('a3','sudo systemd-run --unit='+unit+' --on-active=4min /bin/systemctl start crawl-node-exporter')
   started=time.monotonic()
   try:
    nodes.run('a3','sudo systemctl stop crawl-node-exporter')
    wait(lambda:bool(query(p0,'ALERTS{alertname="MonitoringTargetDown",node="a3",alertstate="firing"}')) and bool(query(p1,'ALERTS{alertname="MonitoringTargetDown",node="a3",alertstate="firing"}')),180)
    active=wait(lambda:alerts(am),60)
    assert len(active)==1 and 'replica' not in active[0]['labels']
    record('alert-fired',seconds=round(time.monotonic()-started,2),deduplicatedAlertCount=len(active))
   finally:
    nodes.run('a3','sudo systemctl start crawl-node-exporter')
    nodes.run('a3','sudo systemctl stop '+unit+'.timer')
   restored=time.monotonic()
   wait(lambda:ready(p0) and ready(p1) and not alerts(am),180)
   record('alert-resolved',seconds=round(time.monotonic()-restored,2))
   timestamp=int(time.time())-60
   before=query(p0,'up{job="nodes",node="a1"} @ '+str(timestamp));assert before
   before_value=before[0]['value'][1]
   old=json.loads(kube('get','pod','prometheus-0','-o','json'))
   pvc=json.loads(kube('get','pvc','data-prometheus-0','-o','json'))['spec']['volumeName']
  kube('delete','pod','prometheus-0','--wait=false')
  wait(lambda:json.loads(kube('get','pod','prometheus-0','-o','json'))['metadata']['uid']!=old['metadata']['uid'],120)
  with forward('prometheus-0',28090,9090) as p0:
   wait(lambda:ready(p0))
   after=query(p0,'up{job="nodes",node="a1"} @ '+str(timestamp));assert after and after[0]['value'][1]==before_value
   assert json.loads(kube('get','pvc','data-prometheus-0','-o','json'))['spec']['volumeName']==pvc
   record('prometheus-replacement',historicalSampleTimestamp=timestamp,historicalValue=before_value,persistentVolume=pvc,peerContinuedScraping=ready(p1))
   # After a restart, old ALERTS samples can remain in the query lookback window.
   # Require the rule engine and instant series to settle before final acceptance.
   def settled():
    groups=get(p0,'/api/v1/rules')['data']['groups']
    live=[r for g in groups for r in g['rules'] if r.get('state')=='firing' and r['name']!='SeaweedContinuousBackupPending']
    series=query(p0,'ALERTS{alertstate="firing"}')
    return not live and {a['metric']['alertname'] for a in series}<={'SeaweedContinuousBackupPending'}
   wait(settled,360)
   firing=query(p0,'ALERTS{alertstate="firing"}')
   record('remaining-alerts',alerts=[a['metric']['alertname'] for a in firing])
   assert {a['metric']['alertname'] for a in firing}<={'SeaweedContinuousBackupPending'}
 evidence.update(status='PASSED',completedAt=datetime.now(timezone.utc).isoformat())
 out=ROOT/'ops/checks/2026-09-21-monitoring-evidence.json';out.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n');print(str(out),flush=True)

if __name__=='__main__':main()
