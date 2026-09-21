#!/usr/bin/env python3
"""S3 root; stdin JSON names encrypted backup and exact probe expectations.
Runs a new ClickHouse instance on loopback 18123/19001 with isolated data/config.
Does not restore into or stop the live ClickHouse instance.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time
import uuid

def run(args,**kwargs):return subprocess.run(args,check=True,capture_output=True,timeout=120,**kwargs)

def main():
    assert os.geteuid()==0;os.umask(0o077)
    spec=json.load(sys.stdin)
    assert re.fullmatch(r'clickhouse-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip\.gpg',spec['file'])
    assert re.fullmatch(r'infra_backup_probe_[a-f0-9]{12}',spec['table'])
    archive=Path('/srv/crawlsystem/backups/clickhouse/restore-input')/spec['file']
    with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==spec['sha256']
    available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))
    assert available>1536*1024,'Insufficient free memory for isolated recovery test'
    work=Path('/srv/crawlsystem/restore-check-ch-'+uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    unit='crawl-ch-restore-'+work.name[-12:]
    started=False;start=time.monotonic()
    try:
        for folder in ['data','tmp','user_files','format_schemas','backups']:(work/folder).mkdir()
        native=work/'backups/restore.zip'
        run(['gpg','--batch','--pinentry-mode','loopback','--passphrase-file','/etc/crawl-backup/control-cipher-pass','--output',str(native),'--decrypt',str(archive)])
        with native.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==spec['nativeSha256']
        config=f'''<clickhouse>
<logger><level>warning</level><console>true</console></logger>
<listen_host>127.0.0.1</listen_host><http_port>18123</http_port><tcp_port>19001</tcp_port>
<path>{work}/data/</path><tmp_path>{work}/tmp/</tmp_path>
<user_files_path>{work}/user_files/</user_files_path><format_schema_path>{work}/format_schemas/</format_schema_path>
<max_server_memory_usage>805306368</max_server_memory_usage><max_concurrent_queries>4</max_concurrent_queries>
<max_thread_pool_size>128</max_thread_pool_size><background_pool_size>16</background_pool_size>
<background_schedule_pool_size>2</background_schedule_pool_size>
<background_buffer_flush_schedule_pool_size>1</background_buffer_flush_schedule_pool_size>
<background_message_broker_schedule_pool_size>1</background_message_broker_schedule_pool_size>
<background_distributed_schedule_pool_size>1</background_distributed_schedule_pool_size>
<mark_cache_size>16777216</mark_cache_size><uncompressed_cache_size>0</uncompressed_cache_size>
<backup_threads>2</backup_threads><max_backups_io_thread_pool_size>4</max_backups_io_thread_pool_size>
<profiles><default><max_threads>2</max_threads><max_memory_usage>268435456</max_memory_usage></default></profiles>
<users><default><password></password><networks><ip>127.0.0.1</ip></networks><profile>default</profile><quota>default</quota></default></users>
<quotas><default><interval><duration>3600</duration><queries>0</queries><errors>0</errors><result_rows>0</result_rows><read_rows>0</read_rows><execution_time>0</execution_time></interval></default></quotas>
<storage_configuration><disks><restore><type>local</type><path>{work}/backups/</path></restore></disks></storage_configuration>
<backups><allowed_disk>restore</allowed_disk></backups>
</clickhouse>'''
        (work/'config.xml').write_text(config)
        user=pwd.getpwnam('clickhouse')
        for root,dirs,files in os.walk(work):
            os.chown(root,user.pw_uid,user.pw_gid)
            for name in files:os.chown(Path(root)/name,user.pw_uid,user.pw_gid)
        run(['systemd-run','--unit='+unit,'--property=User=clickhouse','--property=Group=clickhouse',
             '--property=RuntimeMaxSec=300','--property=TimeoutStopSec=30','--property=MemoryMax=1G',
             '/usr/bin/clickhouse','server','--config-file='+str(work/'config.xml')]);started=True
        def sql(statement):return run(['clickhouse-client','--host','127.0.0.1','--port','19001','--query',statement],text=True).stdout.strip()
        for _ in range(60):
            try:
                if sql('SELECT 1')=='1':break
            except subprocess.CalledProcessError:pass
            time.sleep(.5)
        else:raise RuntimeError('Isolated ClickHouse did not start; inspect transient unit')
        restored=json.loads(sql("RESTORE DATABASE crawler_analytics FROM Disk('restore','restore.zip') FORMAT JSONEachRow"))
        assert restored['status']=='RESTORED',restored
        actual=sql('SELECT * FROM crawler_analytics.'+spec['table']+' ORDER BY event_id,revision FORMAT JSONEachRow')
        assert actual==spec['expectedRows'],'Restored rows differ from snapshot'
        tables=[json.loads(line) for line in sql("SELECT name,engine FROM system.tables WHERE database='crawler_analytics' ORDER BY name FORMAT JSONEachRow").splitlines()]
        assert tables==spec['tables'],'Restored table inventory differs'
        assert sql('SELECT count() FROM crawler_analytics.'+spec['table']+' WHERE event_id=999')=='0','Post-backup row leaked into snapshot'
        assert sql("SELECT path FROM system.disks WHERE name='default'").startswith(str(work)+'/')
        result={'status':'PASSED','file':spec['file'],'version':sql('SELECT version()'),
            'tableInventoryMatches':True,'tables':tables,'probeRows':len(actual.splitlines()),
            'probeSha256':hashlib.sha256(actual.encode()).hexdigest(),'postBackupRowExcluded':True,
            'restoreStatus':restored['status'],'elapsedSeconds':round(time.monotonic()-start,2),
            'isolation':'fresh data directory and config; loopback HTTP 18123/Native 19001; live instance untouched'}
    finally:
        if started:run(['systemctl','stop',unit])
        shutil.rmtree(work)
    archive.unlink()
    result['cleanup']='isolated instance stopped; temporary data and copied archive removed'
    print(json.dumps(result))

if __name__=='__main__':main()
