#!/usr/bin/env python3
"""Bound only the derived log table; never delete business/PG/Kafka data."""
import fcntl,json,os,re,shutil,subprocess,time
from datetime import datetime,timezone,timedelta
from pathlib import Path
LIMIT=2*1024**3
TARGET=1536*1024**2
STATE=Path('/var/lib/crawl-log-retention/status.json')

def sql(q):return subprocess.check_output(['clickhouse-client','--host','127.0.0.1','--query',q],text=True,timeout=20)
def choose(parts,free_ratio,now):
 cutoff=(now-timedelta(days=3)).strftime('%Y%m%d%H')
 total=sum(p['bytes'] for p in parts)
 capacity_pressure=total>LIMIT
 dropped=[]
 for p in sorted(parts,key=lambda x:x['partition']):
  assert re.fullmatch(r'\d{10}',p['partition'])
  if p['partition']<cutoff or (capacity_pressure and total>TARGET) or free_ratio<0.15:
   dropped.append(p['partition']);total-=p['bytes']
 return dropped

def main():
 STATE.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 with open(STATE.parent/'.lock','w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  parts=json.loads(sql("SELECT partition, toUInt64(sum(bytes_on_disk)) AS bytes FROM system.parts WHERE database='crawler_logs' AND table='events' AND active GROUP BY partition FORMAT JSON"))['data']
  for p in parts:p['bytes']=int(p['bytes'])
  disk=shutil.disk_usage('/var/lib/clickhouse');ratio=disk.free/disk.total
  selected=choose(parts,ratio,datetime.now(timezone.utc))
  old=json.loads(STATE.read_text()) if STATE.exists() else {}
  for part in selected:sql("ALTER TABLE crawler_logs.events DROP PARTITION '"+part+"'")
  result={'checked_at':time.time(),'active_bytes_before':sum(p['bytes'] for p in parts),'dropped_partitions_total':old.get('dropped_partitions_total',0)+len(selected),'dropped_this_run':len(selected),'free_ratio':ratio,'limit_bytes':LIMIT,'success':True}
  temp=STATE.with_suffix('.tmp');temp.write_text(json.dumps(result));os.chmod(temp,0o600);temp.replace(STATE)
  print(json.dumps({'partitionsDropped':len(selected),'activeBytesBefore':result['active_bytes_before']}))
if __name__=='__main__':main()
