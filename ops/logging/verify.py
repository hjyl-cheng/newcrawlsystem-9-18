#!/usr/bin/env python3
"""Bounded live logging verification. Fault injection affects A3 -> S3:8443 only."""
import argparse,base64,importlib.util,json,shlex,ssl,subprocess,time,urllib.error,urllib.request,urllib.parse,uuid
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('backend',ROOT/'ops/logging/prepare-backend.py');b=importlib.util.module_from_spec(s);s.loader.exec_module(b)

def rows(query):return json.loads(b.sql(query+' FORMAT JSON'))['data']
def wait(fn,seconds=120):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  result=fn()
  if result:return result
  time.sleep(3)
 raise RuntimeError('Logging verification timed out')
def emit(node,messages):
 b.nodes.run(node,'sudo systemd-run --unit=crawl-log-probe --collect --wait /usr/bin/printf '+shlex.quote('%s\n')+' '+shlex.join(messages))
def auth(user):
 return {'Authorization':'Basic '+base64.b64encode((user+':'+(b.PRIVATE/(user+'.password')).read_text().strip()).encode()).decode()}
def http_sql(user,query):
 c=ssl.create_default_context(cafile=str(b.PRIVATE/'ca.crt'))
 req=urllib.request.Request('https://10.4.4.5:8443/',data=query.encode(),headers=auth(user))
 try:
  with urllib.request.urlopen(req,context=c,timeout=15) as r:return r.status,r.read().decode()
 except urllib.error.HTTPError as e:return e.code,e.read().decode()
def kube(*args):return subprocess.check_output(['kubectl',*args],text=True)
def gf(path,body=None):
 password=(ROOT/'secrets/monitoring/grafana-admin-password').read_text().strip()
 headers={'Authorization':'Basic '+base64.b64encode(('admin:'+password).encode()).decode()}
 if body is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request('http://10.107.82.103:3000'+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
 with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true',required=True);p.add_argument('--resume-outage',action='store_true');args=p.parse_args()
 marker='CRAWLLOG-'+uuid.uuid4().hex[:16]
 evidence={'startedAt':datetime.now(timezone.utc).isoformat(),'marker':marker,'phases':[]}
 output=ROOT/'ops/checks/2026-09-21-logging-evidence.json'
 if args.resume_outage:
  evidence=json.loads(output.read_text());marker=evidence['marker']
  assert [x['phase'] for x in evidence['phases']]==['six-node-journal','kubernetes-stdout','access-boundaries','grafana-query']
  assert time.time()-output.stat().st_mtime<1800
 def record(phase,**data):
  evidence['phases'].append({'phase':phase,**data});output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n');print(json.dumps(evidence['phases'][-1],ensure_ascii=False),flush=True)
 if not args.resume_outage:
  health=gf('/api/datasources/uid/clickhouse-logs/health');assert health['status']=='OK'
  assert len(gf('/api/dashboards/uid/crawl-logs')['dashboard']['panels'])==2
  for n in b.nodes.NODES:emit(n,[marker+'-'+n+' password=fixture-never-store token="fixture-token"'])
  def journal_arrived():
   r=rows("SELECT Node, Body, Source FROM crawler_logs.events WHERE startsWith(Body,'"+marker+"-') AND Service='crawl-log-probe'")
   return r if {x['Node'] for x in r}==set(b.nodes.NODES) else None
  journal=wait(journal_arrived)
  assert all('fixture-never-store' not in r['Body'] and 'fixture-token' not in r['Body'] and 'REDACTED' in r['Body'] and r['Source']=='journal' for r in journal)
  record('six-node-journal',nodes=sorted({r['Node'] for r in journal}),redactionVerified=True)
  image=json.loads((ROOT/'ops/monitoring/images.json').read_text())['python']['image'];podname='logging-probe-'+uuid.uuid4().hex[:8]
  pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':podname,'namespace':'crawl-monitoring','labels':{'app':'logging-probe'}},'spec':{'automountServiceAccountToken':False,'restartPolicy':'Never','activeDeadlineSeconds':120,'nodeSelector':{'kubernetes.io/hostname':'a2'},'containers':[{'name':'probe','image':image,'command':['python3','-c',"print('"+marker+"-pod password=fixture-pod-secret',flush=True)"],'resources':{'requests':{'cpu':'10m','memory':'16Mi'},'limits':{'cpu':'100m','memory':'64Mi'}},'securityContext':{'runAsUser':65534,'runAsNonRoot':True,'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}}]}}
  subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(pod),text=True,check=True,capture_output=True)
  try:
   r=wait(lambda:rows("SELECT Node, Namespace, Pod, Container, Source, Body FROM crawler_logs.events WHERE Pod='"+podname+"' AND startsWith(Body,'"+marker+"-pod')"))
   assert r[0]['Node']=='a2' and r[0]['Source']=='kubernetes' and 'fixture-pod-secret' not in r[0]['Body']
   record('kubernetes-stdout',node='a2',namespace=r[0]['Namespace'],container=r[0]['Container'],redactionVerified=True)
  finally:kube('-n','crawl-monitoring','delete','pod',podname,'--ignore-not-found','--wait=false')
  assert http_sql('crawl_logs_grafana','SELECT count() FROM crawler_logs.events')[0]==200
  assert http_sql('crawl_logs_a1','SELECT 1')[0]==200
  status,_=http_sql('crawl_logs_a1','SELECT count() FROM crawler_logs.events');assert status!=200
  status,_=http_sql('crawl_logs_grafana','INSERT INTO crawler_logs.events FORMAT JSONEachRow');assert status!=200
  protected='acl_probe_'+uuid.uuid4().hex[:8]
  b.sql('CREATE TABLE crawler_logs.'+protected+' (Marker UInt8) ENGINE=Memory;')
  try:
   status,_=http_sql('crawl_logs_grafana','SELECT count() FROM crawler_logs.'+protected);assert status!=200
  finally:b.sql('DROP TABLE crawler_logs.'+protected)
  bad_tls=False
  try:urllib.request.urlopen('https://10.4.4.5:8443/ping',timeout=3)
  except urllib.error.URLError as error:bad_tls=isinstance(error.reason,ssl.SSLCertVerificationError)
  assert bad_tls
  record('access-boundaries',writerCannotRead=True,readerCannotWrite=True,readerCannotReadOtherTable=True,untrustedCaRejected=True)
  response=gf('/api/ds/query',{'from':str(int((time.time()-3600)*1000)),'to':str(int(time.time()*1000)),'queries':[{'refId':'A','datasource':{'uid':'clickhouse-logs','type':'grafana-clickhouse-datasource'},'queryType':'sql','editorType':'sql','format':2,'rawSql':"SELECT Timestamp AS timestamp, Body AS body, SeverityText AS level, Node, Service FROM crawler_logs.events WHERE startsWith(Body,'"+marker+"-') ORDER BY Timestamp DESC LIMIT 200"}]})
  assert 'error' not in response['results']['A'] and response['results']['A']['frames']
  record('grafana-query',datasourceHealth='OK',frames=len(response['results']['A']['frames']))
 restore='''#!/bin/sh
iptables -D OUTPUT -j CRAWL_LOG_VERIFY 2>/dev/null || true
iptables -F CRAWL_LOG_VERIFY 2>/dev/null || true
iptables -X CRAWL_LOG_VERIFY 2>/dev/null || true
'''
 b.nodes.put('a3','/run/crawl-log-verify-restore.sh',restore,mode=0o700)
 timer='crawl-log-verify-rescue-'+str(int(time.time()))
 b.nodes.run('a3','sudo systemd-run --unit='+timer+' --on-active=4min /bin/sh /run/crawl-log-verify-restore.sh')
 outage_messages=[marker+'-outage-'+str(i) for i in range(12)]
 started=time.monotonic()
 try:
  b.nodes.run('a3','sudo iptables -N CRAWL_LOG_VERIFY && sudo iptables -A CRAWL_LOG_VERIFY -d 10.4.4.5/32 -p tcp --dport 8443 -j REJECT && sudo iptables -I OUTPUT 1 -j CRAWL_LOG_VERIFY')
  emit('a3',outage_messages)
  time.sleep(18)
  assert not rows("SELECT Body FROM crawler_logs.events WHERE startsWith(Body,'"+marker+"-outage-')")
  b.nodes.run('a3','sudo systemctl restart crawl-vector')
  active=b.nodes.run('a3','systemctl is-active kubelet containerd crawl-vector').stdout.strip().splitlines();assert active==['active']*3
  current=json.loads(kube('get','nodes','-o','json'));assert all(any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions']) for n in current['items'])
  record('sink-outage',agentRestartedWhileBlocked=True,nodeServicesStayedActive=True,seconds=round(time.monotonic()-started,2))
 finally:
  b.nodes.run('a3','sudo /bin/sh /run/crawl-log-verify-restore.sh && sudo systemctl stop '+timer+'.timer')
 restored=time.monotonic()
 def recovered():
  r=rows("SELECT Body, count() AS n FROM crawler_logs.events WHERE startsWith(Body,'"+marker+"-outage-') GROUP BY Body")
  return r if {x['Body'] for x in r}==set(outage_messages) else None
 delivered=wait(recovered,180)
 record('recovery',uniqueMessages=len(delivered),observedDuplicates=sum(int(r['n'])-1 for r in delivered),seconds=round(time.monotonic()-restored,2))
 freshness=rows("SELECT Node,dateDiff('second',max(IngestedAt),now()) AS age FROM crawler_logs.events WHERE Source='heartbeat' GROUP BY Node ORDER BY Node")
 assert len(freshness)==6 and all(int(r['age'])<180 for r in freshness)
 evidence.update(status='PASSED',completedAt=datetime.now(timezone.utc).isoformat(),heartbeats=freshness)
 output.write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n');print('PASSED: '+str(output),flush=True)
if __name__=='__main__':main()
