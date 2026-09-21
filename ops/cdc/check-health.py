#!/usr/bin/env python3
"""Read-only CDC readiness/WAL inspection. Does not delete slots or send alerts."""
import json,subprocess,time,shlex
from datetime import datetime,timezone
import common as c
NAME='crawl-publication-validation'
def rest(path):
 r=subprocess.check_output(['kubectl','-n','crawl-validation','exec','deployment/kafka-connect','--','curl','-fsS','http://127.0.0.1:8083'+path],text=True,timeout=30)
 return json.loads(r)
def main():
 result={'checkedAt':datetime.now(timezone.utc).isoformat(),'nodes':{}};issues=[]
 status=rest('/connectors/'+NAME+'/status')
 result['connector']={'state':status['connector']['state'],'tasks':[{'id':t['id'],'state':t['state'],'worker':t['worker_id']} for t in status['tasks']]}
 if status['connector']['state']!='RUNNING' or not status['tasks'] or any(t['state']!='RUNNING' for t in status['tasks']):issues.append('Connector or task not running')
 primary=c.leader();result['primary']=primary
 scope=c.sql(primary,"SELECT schemaname||'.'||tablename FROM pg_publication_tables WHERE pubname='crawl_cdc_validation' ORDER BY 1;",'crawler');result['publication']=scope
 if scope!='cdc_validation.outbox':issues.append('Publication is not the exact allowed table')
 for name in c.NODES:
  statement="SELECT json_build_object('slot',slot_name,'failover',failover,'synced',synced,'temporary',temporary,'active',active,'restart_lsn',restart_lsn,'confirmed_flush_lsn',confirmed_flush_lsn,'wal_status',wal_status,'safe_wal_size',safe_wal_size,'invalidation_reason',invalidation_reason,'retained_bytes',pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END,restart_lsn)) FROM pg_replication_slots WHERE slot_name='crawl_cdc_validation';"
  raw=c.sql(name,statement);slot=json.loads(raw) if raw else None
  result['nodes'][name]={'slot':slot,'pgWalBytes':int(c.sql(name,'SELECT coalesce(sum(size),0) FROM pg_ls_waldir();'))}
  if not slot or not slot['failover'] or slot['temporary'] or slot['invalidation_reason'] or slot['wal_status'] not in ['reserved','extended'] or (name!=primary and not slot['synced']):issues.append(name+': failover slot is not ready')
  if slot and (slot.get('retained_bytes') or 0)>1024**3:issues.append(name+': retained WAL exceeds 1 GiB warning threshold')
  guard=json.loads(c.run(c.NODES[name],'sudo cat /var/lib/crawl-cdc-guard/status.json',capture=True).stdout)
  result['nodes'][name]['guard']=guard
  if time.time()-guard['checked_at']>15 or guard['state']!=('READY' if name==primary else 'STANDBY_READY'):issues.append(name+': CDC guard is not ready or status is stale')
 command="import runpy,json;m=runpy.run_path('/opt/crawlsystem/cdc/ha-guard.py');g=m['Guard']();s=g.dcs.snapshot();print(json.dumps({'leader':s['leader'],'policy':s['policy'],'barrier':sorted(g.barrier())}))"
 fence=json.loads(c.run(c.NODES[primary],'sudo -u postgres python3 -c '+shlex.quote(command),capture=True).stdout)
 result['candidateFence']=fence
 if not fence['policy'] or fence['leader']['value']!=primary or fence['policy']['owner']!=primary or set(fence['policy']['allowed'])!=set(fence['barrier']):issues.append('Primary CDC policy and active WAL barrier do not match')
 result['connectOffsets']=rest('/connectors/'+NAME+'/offsets')
 result['status']='ATTENTION' if issues else 'PASSED';result['issues']=issues
 result['scope']='On-demand readiness and WAL budget check; not continuous alerts, whole-host failover certification, or proof of end-to-end business delivery'
 print(json.dumps(result,indent=2));return bool(issues)
if __name__=='__main__':raise SystemExit(main())
