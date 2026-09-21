#!/usr/bin/python3
"""Bounded read-only infrastructure probes -> node_exporter textfile collector.
Runs as root to read protected backup metadata; exports no credentials or logs.
"""
import json,os,re,socket,subprocess,time,urllib.request
from datetime import datetime
from pathlib import Path

DEST=Path('/var/lib/crawl-node-exporter/textfile/crawl.prom')
NODE=socket.gethostname().split('.')[0]

def command(args,timeout=8):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout).stdout.strip()

class Metrics:
    def __init__(self):self.lines=[]
    def add(self,name,value,**labels):
        labels={'node':NODE,**labels}
        encoded=','.join(k+'='+json.dumps(str(v)) for k,v in sorted(labels.items()))
        self.lines.append(name+'{'+encoded+'} '+str(float(value)))
    def section(self,name,fn):
        before=len(self.lines)
        try:fn(self);ok=1
        except Exception as error:
            self.lines=self.lines[:before];ok=0
            print(json.dumps({'collector':name,'error_type':type(error).__name__}),flush=True)
        self.add('crawl_collector_success',ok,collector=name)

def services(m):
    units=['kubelet','containerd','crawl-kube-api'] if NODE.startswith('a') else ['crawl-patroni','crawl-pg-etcd','crawl-cdc-guard','pgbouncer','kafka','seaweedfs-master','seaweedfs-volume','seaweedfs-filer','seaweedfs-s3','crawl-seaweed-pg']
    if NODE=='s3':units.append('clickhouse-server')
    if Path('/etc/crawl-vector/vector.yaml').exists():units.append('crawl-vector')
    for unit in units:
        state=command(['systemctl','show',unit+'.service','-p','ActiveState','--value'])
        m.add('crawl_service_up',state=='active',service=unit)

def pg(m):
    sql="""SELECT json_build_object(
      'primary',NOT pg_is_in_recovery(),'connections',(SELECT count(*) FROM pg_stat_activity),
      'max_connections',current_setting('max_connections')::int,
      'wal_bytes',(SELECT coalesce(sum(size),0) FROM pg_ls_waldir()),
      'archive_failures',(SELECT failed_count FROM pg_stat_archiver),
      'last_archived',(SELECT extract(epoch from last_archived_time) FROM pg_stat_archiver),
      'replay_pending',CASE WHEN pg_is_in_recovery() THEN coalesce(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),pg_last_wal_replay_lsn()),0) ELSE 0 END,
      'barrier',current_setting('synchronized_standby_slots'),
      'slots',(SELECT coalesce(json_agg(json_build_object('name',slot_name,'type',slot_type,
        'active',active,'synced',synced,'temporary',temporary,'failover',failover,
        'valid',invalidation_reason IS NULL AND wal_status IN ('reserved','extended'),
        'safe_wal_size',safe_wal_size,
        'retained_bytes',pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn() ELSE pg_current_wal_lsn() END,restart_lsn))), '[]'::json)
        FROM pg_replication_slots WHERE slot_name IN ('s1','s2','s3','crawl_cdc_validation')),
      'replicas',(SELECT coalesce(json_agg(json_build_object('node',application_name,
        'replay_lag_bytes',greatest(pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn),0),
        'sync',sync_state='sync')), '[]'::json) FROM pg_stat_replication WHERE application_name IN ('s1','s2','s3'))
    );"""
    r=json.loads(command(['runuser','-u','postgres','--','psql','-XAt','-v','ON_ERROR_STOP=1','-c',"SET statement_timeout='3s'; "+sql]).splitlines()[-1])
    for key,metric in [('primary','is_primary'),('connections','connections'),('max_connections','max_connections'),('wal_bytes','wal_bytes'),('archive_failures','archive_failures_total'),('last_archived','last_archived_timestamp_seconds'),('replay_pending','replay_pending_bytes')]:
        m.add('crawl_pg_'+metric,r[key] or 0)
    names={s['name'] for s in r['slots']}
    m.add('crawl_pg_cdc_slot_present','crawl_cdc_validation' in names)
    for s in r['slots']:
        for key in ['active','synced','temporary','failover','valid','retained_bytes','safe_wal_size']:
            if s[key] is not None:m.add('crawl_pg_slot_'+key,s[key],slot=s['name'],slot_type=s['type'])
    m.add('crawl_pg_streaming_replicas',len(r['replicas']))
    m.add('crawl_pg_synchronous_replicas',sum(bool(x['sync']) for x in r['replicas']))
    for replica in r['replicas']:m.add('crawl_pg_replica_replay_lag_bytes',replica['replay_lag_bytes'] or 0,replica=replica['node'])
    actual={n.strip() for n in r['barrier'].split(',') if n.strip()}
    m.add('crawl_cdc_barrier_members',len(actual))
    if r['primary']:
        state=json.loads(Path('/var/lib/crawl-cdc-guard/status.json').read_text())
        m.add('crawl_cdc_guard_barrier_matches',actual==set(state.get('candidates',[])))

def guard(m):
    r=json.loads(Path('/var/lib/crawl-cdc-guard/status.json').read_text())
    m.add('crawl_cdc_guard_last_check_timestamp_seconds',r['checked_at'])
    m.add('crawl_cdc_guard_ready',r['state'] in ['READY','STANDBY_READY'])
    m.add('crawl_cdc_guard_candidates',len(r.get('candidates',[])))

def backup_file(m,kind,folder,timer,service):
    r=json.loads((folder/'last-success.json').read_text());archive=folder/r['file']
    if kind=='control':stamp=datetime.strptime(r['file'],'control-%Y%m%dT%H%M%SZ.tar.gpg').replace(tzinfo=__import__('datetime').timezone.utc).timestamp()
    else:stamp=datetime.fromisoformat(r['completedAt']).timestamp()
    m.add('crawl_backup_last_success_timestamp_seconds',stamp,backup=kind)
    m.add('crawl_backup_archive_present',archive.is_file(),backup=kind)
    enabled=subprocess.run(['systemctl','is-enabled',timer],capture_output=True).returncode==0
    active=subprocess.run(['systemctl','is-active',timer],capture_output=True).returncode==0
    m.add('crawl_backup_scheduled',enabled and active,backup=kind)
    result=command(['systemctl','show',service,'-p','Result','--value'])
    m.add('crawl_backup_last_run_success',result=='success',backup=kind)

def pg_backup(m):
    info=json.loads(command(['runuser','-u','pgbackrest','--','pgbackrest','--stanza=crawler','--output=json','info'],timeout=15))[0]
    valid=[b for b in info['backup'] if not b.get('error')]
    m.add('crawl_backup_last_success_timestamp_seconds',max(b['timestamp']['stop'] for b in valid),backup='postgresql')
    m.add('crawl_backup_archive_present',info['status']['code']==0,backup='postgresql')
    active=all(subprocess.run(['systemctl','is-active','crawl-pgbackrest-'+kind+'.timer'],capture_output=True).returncode==0 for kind in ['full','diff'])
    m.add('crawl_backup_scheduled',active,backup='postgresql')
    for kind in ['full','diff']:
        result=command(['systemctl','show','crawl-pgbackrest-'+kind+'.service','-p','Result','--value'])
        m.add('crawl_backup_last_run_success',result=='success',backup='postgresql',schedule=kind)

def storage(m):
    with urllib.request.urlopen('http://127.0.0.1:8888/?limit=1',timeout=3) as response:
        m.add('crawl_storage_endpoint_up',response.status==200,component='seaweedfs-filer')
    if NODE=='s3':
        value=command(['clickhouse-client','--host','127.0.0.1','--receive_timeout','3','--query','SELECT 1'],timeout=5)
        m.add('crawl_storage_endpoint_up',value=='1',component='clickhouse')

def seaweed_backup(m):
    files=list(Path('/srv/crawlsystem/backups/seaweedfs-baseline').glob('seaweed-pg-objects-*.tar.gpg'))
    m.add('crawl_backup_last_success_timestamp_seconds',max(p.stat().st_mtime for p in files) if files else 0,backup='seaweedfs')
    m.add('crawl_backup_archive_present',bool(files),backup='seaweedfs')
    # Explicitly expose the known maintenance-only backup boundary.
    m.add('crawl_backup_scheduled',0,backup='seaweedfs')

def logging_agent(m):
    # Keep bounded component labels, not file names, pod IDs, errors or message bodies.
    with urllib.request.urlopen('http://127.0.0.1:9598/metrics',timeout=3) as response:
        lines=response.read().decode().splitlines()
    names=['buffer_size_bytes','buffer_size_events','buffer_discarded_events_total',
           'component_discarded_events_total','component_errors_total','component_sent_events_total']
    components=['journal','files','heartbeat','normalize','budget','clickhouse']
    values={(metric,component):0 for metric in names for component in components}
    for line in lines:
        found=re.match(r'^vector_([a-z_]+)\{([^}]+)\}\s+([0-9.eE+-]+)',line)
        if not found:continue
        metric,labels,value=found.groups()
        component=re.search(r'(?:^|,)component_id="([a-z]+)"',labels)
        if component and (metric,component[1]) in values:
            values[(metric,component[1])]+=float(value)
    for (metric,component),value in values.items():
        m.add('crawl_vector_'+metric,value,component=component)

def logging_store(m):
    query="SELECT Node, toUnixTimestamp(max(IngestedAt)) AS last FROM crawler_logs.events WHERE Source='heartbeat' AND IngestedAt > now()-INTERVAL 1 DAY GROUP BY Node FORMAT JSON"
    rows=json.loads(command(['clickhouse-client','--query',query],timeout=5))['data']
    by_node={r['Node']:r['last'] for r in rows}
    for node in ['a1','a2','a3','s1','s2','s3']:
        m.add('crawl_logs_last_heartbeat_timestamp_seconds',by_node.get(node,0),source_node=node)
    value=command(['clickhouse-client','--query',"SELECT coalesce(sum(bytes_on_disk),0) FROM system.parts WHERE database='crawler_logs' AND table='events' AND active"],timeout=5)
    m.add('crawl_logs_active_bytes',int(value))
    state=json.loads(Path('/var/lib/crawl-log-retention/status.json').read_text())
    m.add('crawl_logs_retention_checked_timestamp_seconds',state['checked_at'])
    m.add('crawl_logs_dropped_partitions_total',state['dropped_partitions_total'])

def main():
    if NODE not in ['a1','a2','a3','s1','s2','s3']:raise RuntimeError('Unknown node')
    m=Metrics();m.section('services',services)
    if NODE.startswith('s'):
        m.section('postgresql',pg);m.section('cdc-guard',guard);m.section('storage',storage)
    if NODE=='a1':
        m.section('backup-control',lambda m:backup_file(m,'control',Path('/srv/crawlsystem/backups/control'),'crawl-control-backup.timer','crawl-control-backup.service'))
        m.section('backup-clickhouse',lambda m:backup_file(m,'clickhouse',Path('/srv/crawlsystem/backups/clickhouse'),'crawl-clickhouse-backup.timer','crawl-clickhouse-backup.service'))
    if NODE=='s2':m.section('backup-postgresql',pg_backup)
    if NODE in ['s2','s3']:m.section('backup-seaweedfs',seaweed_backup)
    if Path('/etc/crawl-vector/vector.yaml').exists():m.section('logging-agent',logging_agent)
    if NODE=='s3' and Path('/var/lib/crawl-log-retention').exists():m.section('logging-store',logging_store)
    m.add('crawl_collector_last_run_timestamp_seconds',time.time())
    tmp=DEST.with_suffix('.tmp');tmp.write_text('\n'.join(m.lines)+'\n');os.chmod(tmp,0o644);tmp.replace(DEST)

if __name__=='__main__':main()
