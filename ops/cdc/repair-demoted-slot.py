#!/usr/bin/env python3
"""Operator-triggered repair of a stale *inactive standby* slot after demotion.
Never drops the active primary slot. Excludes the standby from promotion first.
On any failure it deliberately retains nofailover=true for operator inspection.
"""
import argparse,json,time,os
import common as c
SLOT='crawl_cdc_validation'
def state(node):
 raw=c.sql(node,"SELECT json_build_object('recovery',pg_is_in_recovery(),'active',active,'failover',failover,'synced',synced,'temporary',temporary,'confirmed',confirmed_flush_lsn,'restart',restart_lsn,'plugin',plugin,'database',database,'invalid',invalidation_reason) FROM pg_replication_slots WHERE slot_name='"+SLOT+"';")
 return json.loads(raw) if raw else None

def finish(node,previous):
 primary=c.leader();assert primary!=node, 'Never repair the current primary'
 upstream=state(primary)
 assert upstream and upstream['active'] and upstream['failover'] and not upstream['temporary'] and not upstream['invalid']
 assert next(m for m in c.members() if m['name']==node).get('tags',{}).get('nofailover'), 'Promotion exclusion must remain set'
 for _ in range(100):
  after=state(node)
  if after and after['recovery'] and after['synced'] and after['failover'] and not after['temporary'] and not after['invalid'] and after['confirmed']:
   assert after['database']==upstream['database'] and after['plugin']==upstream['plugin']
   if c.sql(node,"SELECT '"+after['confirmed']+"'::pg_lsn >= '"+upstream['confirmed']+"'::pg_lsn;")=='t':break
  time.sleep(1)
 else:raise RuntimeError('Slot not safe; standby remains excluded. Inspect normal decoding progress, then use --resume; never force advance LSN.')
 assert c.leader()==primary, 'Cluster leadership changed during repair; keep excluded'
 ip=c.NODES[node];path='/etc/crawl-patroni/patroni.yml'
 cfg=json.loads(c.run(ip,'sudo cat '+path,capture=True).stdout)
 assert cfg.get('tags',{}).get('nofailover') is True
 cfg['tags']['nofailover']=previous;c.put(ip,path,json.dumps(cfg,indent=2),'postgres');c.run(ip,'sudo systemctl reload crawl-patroni',capture=True)
 for _ in range(30):
  if next(m for m in c.members() if m['name']==node).get('tags',{}).get('nofailover',False)==previous:break
  time.sleep(1)
 else:raise RuntimeError('Original promotion tag not yet observed; inspect Patroni')
 return {'node':node,'upstream':primary,'after':after,'originalNofailover':previous,'promotionExclusion':'set and observed before repair; original tag restored after slot ready'}

def repair(node,resume=False):
 journal=c.ROOT/'secrets/cdc'/('slot-repair-'+node+'.json')
 if resume:
  saved=json.loads(journal.read_text());assert saved['node']==node
  # Resume never drops a slot; it only verifies native recovery and restores the tag.
  result=finish(node,saved['originalNofailover']);result['before']=saved['before']
  journal.unlink();return result
 assert c.leader()!=node
 before=state(node);assert before and before['recovery'] and not before['active'] and before['failover'] and not before['synced']
 assert before['database']=='crawler' and before['plugin']=='pgoutput'
 ip=c.NODES[node];path='/etc/crawl-patroni/patroni.yml'
 cfg=json.loads(c.run(ip,'sudo cat '+path,capture=True).stdout)
 previous=cfg.setdefault('tags',{}).get('nofailover',False)
 journal.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 # Exclusive creation retains the original tag across timeouts; never overwrite it.
 with os.fdopen(os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
  json.dump({'node':node,'originalNofailover':previous,'before':before},f)
 cfg['tags']['nofailover']=True;c.put(ip,path,json.dumps(cfg,indent=2),'postgres');c.run(ip,'sudo systemctl reload crawl-patroni',capture=True)
 for _ in range(20):
  if next(m for m in c.members() if m['name']==node).get('tags',{}).get('nofailover'):break
  time.sleep(1)
 else:raise RuntimeError('Standby exclusion not confirmed; do not drop any slot')
 primary=c.leader();upstream=state(primary)
 assert upstream and not upstream['recovery'] and upstream['failover'] and upstream['active'] and not upstream['invalid'] and not upstream['temporary']
 assert upstream['database']==before['database'] and upstream['plugin']==before['plugin']
 assert c.sql(primary,"SELECT '"+upstream['confirmed']+"'::pg_lsn >= '"+before['confirmed']+"'::pg_lsn;")=='t'
 # Nofailover tag has propagated; recheck recovery inside the destructive query.
 c.sql(node,"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name='"+SLOT+"' AND pg_is_in_recovery() AND NOT active AND NOT synced AND failover;")
 # Native sync worker restores the upstream slot, possibly initially temporary.
 result=finish(node,previous);result['before']=before
 journal.unlink();return result
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--node',choices=c.NODES,required=True);p.add_argument('--execute',required=True,action='store_true');p.add_argument('--resume',action='store_true');args=p.parse_args()
 result=repair(args.node,args.resume)
 (c.ROOT/'ops/checks/2026-09-21-cdc-demoted-slot-repair.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
