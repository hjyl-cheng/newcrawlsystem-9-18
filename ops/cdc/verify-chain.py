#!/usr/bin/env python3
"""Synthetic outbox delivery across a Connect replacement and PG switchovers."""
import argparse,importlib.util,json,subprocess,time,uuid
from pathlib import Path
import common as c
spec=importlib.util.spec_from_file_location('connect_admin',Path(__file__).with_name('connect-admin.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)
NAME='crawl-publication-validation'
SLOT='crawl_cdc_validation'

def connector_ready(previous=None):
 for _ in range(150):
  try:
   status=a.api('/connectors/'+NAME+'/status')
   if status['connector']['state']=='RUNNING' and status['tasks'] and all(t['state']=='RUNNING' for t in status['tasks']) and (not previous or status['tasks'][0]['worker_id']!=previous):return status
  except Exception:pass
  time.sleep(1)
 raise RuntimeError('Connector did not recover')

def slots_ready():
 primary=c.leader();target=c.sql(primary,"SELECT confirmed_flush_lsn FROM pg_replication_slots WHERE slot_name='"+SLOT+"';")
 assert target
 last={}
 for _ in range(75):
  ready=True
  for name in c.NODES:
   raw=c.sql(name,"SELECT json_build_object('failover',failover,'synced',synced,'temporary',temporary,'wal_status',wal_status,'invalidation_reason',invalidation_reason,'confirmed_flush_lsn',confirmed_flush_lsn,'caughtUp',confirmed_flush_lsn >= '"+target+"'::pg_lsn) FROM pg_replication_slots WHERE slot_name='"+SLOT+"';")
   state=json.loads(raw) if raw else {};last[name]=state
   if not state.get('failover') or state.get('temporary') or state.get('invalidation_reason') or not state.get('caughtUp') or (name!=primary and not state.get('synced')):ready=False
  if ready:return {'primary':primary,'minimumConfirmedLSN':target,'nodes':last}
  time.sleep(1)
 raise RuntimeError('Failover slots not safely synchronized: '+json.dumps(last))

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
 a.create_probe();connector_ready()
 run_id=uuid.uuid4().hex
 raw=c.ROOT/('ops/checks/cdc-'+run_id+'.raw.txt')
 expected=[];evidence={'runId':run_id,'status':'RUNNING','phases':[]}
 consumer=None
 def events():
  records=[]
  for line in raw.read_text().splitlines():
   try:row=json.loads(line)
   except json.JSONDecodeError:continue
   if row.get('phase')=='event':records.append(row)
  return records
 def insert(phase):
  rows=[]
  for _ in range(4):
   index=len(expected)+1;identifier=str(uuid.uuid4());channel='channel-'+str(index%2)
   body={'event_id':identifier,'run_id':run_id,'seq':index,'phase':phase,'channel_id':channel}
   rows.append("('"+identifier+"','infra','"+channel+"','Probe','"+json.dumps(body)+"'::jsonb)")
   expected.append(identifier)
  statement="BEGIN; INSERT INTO cdc_validation.outbox(id,aggregatetype,aggregateid,type,payload) VALUES "+','.join(rows)+"; INSERT INTO cdc_validation.excluded_noise VALUES (1,'"+run_id+"') ON CONFLICT(id) DO UPDATE SET value=EXCLUDED.value; COMMIT;"
  c.sql(c.leader(),statement,'crawler')
  for _ in range(100):
   if set(expected).issubset({r['id'] for r in events()}):break
   if consumer.poll() is not None:raise RuntimeError('Consumer exited before all messages arrived; inspect '+str(raw))
   time.sleep(1)
  else:raise RuntimeError('Acknowledged events missing after '+phase)
  time.sleep(2)
  evidence['phases'].append({'phase':phase,'acknowledged':len(expected),'uniqueReceived':len({r['id'] for r in events()}),'offsets':a.api('/connectors/'+NAME+'/offsets')})
  print(phase+': '+str(len(expected))+' acknowledged events received',flush=True)
 try:
  with raw.open('w') as out:
   consumer=subprocess.Popen(['node',str(c.ROOT/'ops/cdc/consume-probe.mjs'),run_id,'16'],stdout=out,stderr=out)
   insert('healthy')
   evidence['slotsBeforeFailure']=slots_ready()
   status=connector_ready();worker=status['tasks'][0]['worker_id']
   pods=json.loads(a.kube('-n','crawl-validation','get','pods','-l','app=kafka-connect','-o','json').stdout)['items']
   target=next(p['metadata']['name'] for p in pods if p['status']['podIP']==worker.split(':')[0])
   start=time.monotonic();a.kube('-n','crawl-validation','delete','pod',target,'--wait=false')
   recovered=connector_ready(worker)
   evidence['connectReplacement']={'oldWorker':worker,'newWorker':recovered['tasks'][0]['worker_id'],'observedSeconds':round(time.monotonic()-start,2),'scope':'controlled Pod deletion; not whole-host loss'}
   insert('connect-replaced')
   evidence['slotsBeforeSwitch']=slots_ready()
   original=c.leader();candidate=next(m['name'] for m in c.members() if m['role']=='sync_standby')
   start=time.monotonic();response=c.api(original,'/switchover',{'leader':original,'candidate':candidate})
   c.wait_members();connector_ready()
   evidence['pgSwitchover']={'from':original,'to':candidate,'response':response,'observedSeconds':round(time.monotonic()-start,2)}
   insert('pg-switched')
   evidence['slotsAfterSwitch']=slots_ready()
   if c.leader()!='s1':
    # Temporarily require both synced replicas to safely return the preferred leader.
    oldcount=c.api('s1','/config')['synchronous_node_count']
    try:
     c.api('s1','/config',{'synchronous_node_count':2},'PATCH')
     for _ in range(60):
      if any(m['name']=='s1' and m['role']=='sync_standby' for m in c.members()):break
      time.sleep(1)
     else:raise RuntimeError('S1 did not become synchronous for return')
     slots_ready();leader=c.leader();c.api(leader,'/switchover',{'leader':leader,'candidate':'s1'});c.wait_members()
    finally:c.api('s1','/config',{'synchronous_node_count':oldcount},'PATCH')
    connector_ready()
   insert('preferred-primary-restored')
   consumer.wait(timeout=20);assert consumer.returncode==0
  evidence['finalSlots']=slots_ready()
  messages=events();assert {r['id'] for r in messages}==set(expected)
  evidence.update(status='PASSED',acknowledged=len(expected),uniqueReceived=len({r['id'] for r in messages}),duplicates=len(messages)-len(expected),finalPrimary=c.leader())
  evidence['events']=messages
  assert c.sql(c.leader(),"SELECT schemaname||'.'||tablename FROM pg_publication_tables WHERE pubname='crawl_cdc_validation';",'crawler')=='cdc_validation.outbox'
  evidence['publicationScope']='cdc_validation.outbox only; excluded_noise transactions executed alongside probes'
  (c.ROOT/'ops/checks/2026-09-21-cdc-chain-failover.json').write_text(json.dumps(evidence,indent=2)+'\n')
  print(json.dumps({k:v for k,v in evidence.items() if k in ['status','acknowledged','uniqueReceived','duplicates','finalPrimary','connectReplacement','pgSwitchover']}))
 finally:
  if consumer and consumer.poll() is None:consumer.terminate();consumer.wait(timeout=15)

if __name__=='__main__':main()
