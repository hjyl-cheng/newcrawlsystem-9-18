#!/usr/bin/env python3
"""Enable PG17 native failover slots with bounded rolling restarts; no slot copying."""
import argparse,json,os,time
from pathlib import Path
import common as c

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
 os.umask(0o077);private=c.ROOT/'secrets/cdc';private.mkdir(mode=0o700,exist_ok=True)
 old=c.api('s1','/config');(private/'pre-cdc-dcs.json').write_text(json.dumps(old,indent=2))
 assert old.get('synchronous_mode_strict') and old.get('synchronous_node_count')==1
 original=c.leader()
 for name,ip in c.NODES.items():
  config_path='/etc/crawl-patroni/patroni.yml'
  config=c.run(ip,'sudo cat '+config_path,capture=True).stdout
  # Existing deployment writes JSON (a YAML subset), do not print credentials.
  parsed=json.loads(config)
  (private/(name+'-pre-cdc-patroni.json')).write_text(config)
  parsed['postgresql']['parameters']['synchronized_standby_slots']=','.join(n for n in c.NODES if n!=name)
  c.put(ip,config_path,json.dumps(parsed,indent=2),'postgres')
  hba='/etc/postgresql/17/main/pg_hba.conf';text=c.run(ip,'sudo cat '+hba,capture=True).stdout
  if '# PG17 native slot sync' not in text:
   text='# PG17 native slot sync: normal postgres DB connection as replication role\n'+''.join('host postgres replicator '+source+'/32 scram-sha-256\n' for source in c.NODES.values())+text
   c.put(ip,hba,text,'postgres',0o640)
  c.run(ip,'sudo systemctl reload crawl-patroni',capture=True)
  c.sql(name,'SELECT pg_reload_conf();')
 config=c.api('s1','/config')
 ignores=config.get('ignore_slots',[])
 rule={'name':'crawl_cdc_validation','type':'logical','database':'crawler','plugin':'pgoutput'}
 if rule not in ignores:ignores.append(rule)
 c.api('s1','/config',{'ignore_slots':ignores,'postgresql':{'parameters':{'wal_level':'logical','hot_standby_feedback':'on','sync_replication_slots':'on'}}},'PATCH')
 time.sleep(6)
 restarts=[]
 # Upgrade async standby first, then the other standby; the primary stays online.
 order=sorted([m for m in c.members() if m['name']!=original],key=lambda m:m['role']=='sync_standby')
 for member in order:
  name=member['name']
  if c.sql(name,'SHOW wal_level;')!='logical':
   c.api(name,'/restart',{});restarts.append(name)
  c.wait_members()
 if c.sql(original,'SHOW wal_level;')!='logical':
  candidate=next(m['name'] for m in c.members() if m['role']=='sync_standby')
  c.api(original,'/switchover',{'leader':original,'candidate':candidate});c.wait_members()
  # Demotion/rejoin normally restarts the old primary using the new parameters.
  if c.sql(original,'SHOW wal_level;')!='logical':c.api(original,'/restart',{});restarts.append(original)
  c.wait_members()
 rows={n:c.sql(n,"SELECT name||'='||setting FROM pg_settings WHERE name IN ('wal_level','sync_replication_slots','hot_standby_feedback','synchronized_standby_slots') ORDER BY name;").splitlines() for n in c.NODES}
 assert all('wal_level=logical' in v and 'sync_replication_slots=on' in v and 'hot_standby_feedback=on' in v for v in rows.values())
 evidence={'status':'PASSED','originalLeader':original,'currentLeader':c.leader(),'restartedStandbys':restarts,'settings':rows,
  'slotOwnership':'native PG17 synchronization; exact validation slot ignored by Patroni',
  'availabilityBoundary':'CDC waits for both physical standby slots on each primary; an unavailable standby can pause CDC until recovery. Core PG retains strict sync=1.'}
 (c.ROOT/'ops/checks/2026-09-21-cdc-pg-prepare.json').write_text(json.dumps(evidence,indent=2)+'\n');print(json.dumps(evidence))
if __name__=='__main__':main()
