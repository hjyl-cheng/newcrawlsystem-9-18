#!/usr/bin/env python3
"""Install the native backup disk on S3 and daily encrypted backup timer on A1."""
import argparse
from pathlib import Path
import runpy
import subprocess
import time

ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run,put=h['run'],h['put']
S3='10.4.4.5'

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
    run(S3,'sudo install -d -m 0750 -o clickhouse -g clickhouse /srv/crawlsystem/backups/clickhouse/native',capture=True)
    put(S3,'/etc/clickhouse-server/config.d/98-crawl-backup.xml',
        (ROOT/'ops/clickhouse/backup-config.xml').read_text(),mode=0o644)
    # Additive disk configuration reload; restart only if disk isn't registered.
    run(S3,"sudo clickhouse-client --query 'SYSTEM RELOAD CONFIG'",capture=True)
    for _ in range(10):
        count=run(S3,"sudo clickhouse-client --query \"SELECT count() FROM system.disks WHERE name='crawl_backups'\"",capture=True).stdout.strip()
        if count=='1':break
        time.sleep(1)
    else:raise RuntimeError('Backup disk not registered; inspect config before scheduling')
    subprocess.run(['sudo','install','-m','0755',str(ROOT/'ops/clickhouse/backup-clickhouse.py'),'/opt/crawlsystem/backup/backup-clickhouse.py'],check=True)
    subprocess.run(['sudo','install','-m','0755',str(ROOT/'ops/clickhouse/check-health.py'),'/opt/crawlsystem/backup/check-clickhouse.py'],check=True)
    for name in ['crawl-clickhouse-backup.service','crawl-clickhouse-backup.timer']:
        subprocess.run(['sudo','install','-m','0644',str(ROOT/'ops/clickhouse'/name),'/etc/systemd/system/'+name],check=True)
    subprocess.run(['sudo','systemctl','daemon-reload'],check=True)
    # Enable scheduling only after an end-to-end backup/restore has passed.
    print('S3 native backup disk and A1 units installed; timer not enabled yet')

if __name__=='__main__':main()
