#!/usr/bin/env python3
"""Read-only ClickHouse/backup health snapshot on A1; no external alert delivery."""
from datetime import datetime,timezone
import hashlib,json,runpy,subprocess
from pathlib import Path
h=runpy.run_path(str(Path(__file__).with_name('backup-clickhouse.py')))
def main():
 issues=[];result={'checkedAt':datetime.now(timezone.utc).isoformat()}
 try:result['version']=h['sql']('SELECT version()')
 except Exception:issues.append('S3 ClickHouse query failed')
 try:
  meta=json.loads((h['DEST']/'last-success.json').read_text())
  age=(datetime.now(timezone.utc)-datetime.fromisoformat(meta['completedAt'])).total_seconds()
  result['backup']={'file':meta['file'],'ageHours':round(age/3600,2),'copies':meta['copies']}
  if age>30*3600:issues.append('Last successful backup older than 30 hours')
  local=h['DEST']/meta['file']
  if h['digest'](local)!=meta['sha256']:issues.append('A1 ciphertext hash mismatch')
  remote=h['remote'](h['S2'],['sudo','-n','sha256sum',str(local)],capture_output=True,text=True).stdout.split()[0]
  if remote!=meta['sha256']:issues.append('S2 ciphertext hash mismatch')
 except Exception:issues.append('Backup metadata or archive verification unavailable')
 for action in ['is-enabled','is-active']:
  r=subprocess.run(['systemctl',action,'crawl-clickhouse-backup.timer'],capture_output=True,text=True)
  result[action]=r.stdout.strip()
  if r.returncode:issues.append('Backup timer '+action+' failed')
 state=subprocess.check_output(['systemctl','show','crawl-clickhouse-backup.service','-p','Result','-p','ExecMainStatus'],text=True)
 result['service']=dict(line.split('=',1) for line in state.splitlines())
 if result['service'].get('Result')!='success' or result['service'].get('ExecMainStatus')!='0':issues.append('Last backup service invocation failed')
 result['status']='ATTENTION' if issues else 'PASSED';result['issues']=issues
 result['scope']='On-demand checks only; no centralized metrics or external notification'
 print(json.dumps(result,indent=2));return bool(issues)
if __name__=='__main__':raise SystemExit(main())
