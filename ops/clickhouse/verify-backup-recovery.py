#!/usr/bin/env python3
"""A1 operator: native backup, encrypted cross-host copy, isolated restore.
Creates and finally drops only a unique synthetic probe table.
"""
import argparse,hashlib,json,runpy,shlex,subprocess,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run,put,SSH=h['run'],h['put'],h['SSH']
S3='10.4.4.5'
def sql(statement):
 return run(S3,'sudo clickhouse-client --query '+shlex.quote(statement),capture=True).stdout.strip()
def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
 table='infra_backup_probe_'+uuid.uuid4().hex[:12];fq='crawler_analytics.'+table;created=False
 try:
  sql('CREATE TABLE '+fq+" (event_id UInt64,revision UInt32,observed_at DateTime64(3,'UTC'),payload String,value Nullable(Int64),tags Array(String),amount Decimal(12,2)) ENGINE=MergeTree ORDER BY (event_id,revision)");created=True
  sql('INSERT INTO '+fq+" VALUES (1,1,'2026-09-21 00:00:00.123','中文备份验证',42,['first','unicode'],12.34),(1,2,'2026-09-21 00:01:00.456','revision two',NULL,[],0.00),(2,1,'2026-09-21 00:02:00.789','another event',-7,['third'],-9.87)")
  expected=sql('SELECT * FROM '+fq+' ORDER BY event_id,revision FORMAT JSONEachRow')
  subprocess.run(['sudo','systemctl','start','crawl-clickhouse-backup.service'],check=True)
  backup=json.loads(subprocess.check_output(['sudo','cat','/srv/crawlsystem/backups/clickhouse/last-success.json'],text=True))
  assert any(t['name']==table for t in backup['tables']),'Latest backup does not contain this run'
  sql('INSERT INTO '+fq+" VALUES (999,1,'2026-09-21 00:03:00.000','after snapshot',999,[],999.00)")
  assert sql('SELECT count() FROM '+fq)=='4'
  folder='/srv/crawlsystem/backups/clickhouse/restore-input'
  run(S3,'sudo install -d -m 0700 '+folder,capture=True)
  target=folder+'/'+backup['file']
  receiver='import os,sys;f=open(sys.argv[1],"xb");os.chmod(sys.argv[1],0o600)\nwhile b:=sys.stdin.buffer.read(1048576):f.write(b)\nf.flush();os.fsync(f.fileno());f.close()'
  reader=subprocess.Popen(['sudo','cat','/srv/crawlsystem/backups/clickhouse/'+backup['file']],stdout=subprocess.PIPE)
  try:
   subprocess.run(SSH+['ubuntu@'+S3,shlex.join(['sudo','python3','-c',receiver,target])],stdin=reader.stdout,check=True)
   reader.stdout.close();assert reader.wait(timeout=30)==0
  finally:
   if reader.poll() is None:reader.terminate();reader.wait(timeout=10)
  put(S3,'/tmp/crawl-clickhouse-verify-restore.py',(ROOT/'ops/clickhouse/verify-restore.py').read_text())
  spec={key:backup[key] for key in ['file','sha256','nativeSha256','tables']};spec.update(table=table,expectedRows=expected)
  try:result=run(S3,'sudo python3 /tmp/crawl-clickhouse-verify-restore.py',json.dumps(spec),capture=True)
  except subprocess.CalledProcessError as error:
   print(error.stderr or 'Isolated restore failed');raise RuntimeError('Inspect isolated restore error above') from None
  run(S3,"sudo python3 -c \"from pathlib import Path;Path('/tmp/crawl-clickhouse-verify-restore.py').unlink(missing_ok=True)\"",capture=True)
  restored=json.loads(result.stdout)
  assert restored['probeSha256']==hashlib.sha256(expected.encode()).hexdigest()
  assert sql('SELECT count() FROM '+fq)=='4';restored['liveRowsUnchanged']=4
  (ROOT/'ops/checks/2026-09-21-clickhouse-backup-recovery.json').write_text(json.dumps({'status':'PASSED','backup':backup,'restore':restored},indent=2)+'\n')
  print(json.dumps({'status':'PASSED','restoredRows':restored['probeRows'],'postBackupRowExcluded':True,'liveRowsUnchanged':4,'file':backup['file']}))
 finally:
  if created:sql('DROP TABLE '+fq+' SYNC')
if __name__=='__main__':main()
