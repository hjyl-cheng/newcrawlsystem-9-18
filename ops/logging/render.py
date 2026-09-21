#!/usr/bin/env python3
"""Generate credential-free, bounded per-host Vector configurations."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'ops/logging'
NODES=['a1','a2','a3','s1','s2','s3']
def render(node):
 units=['kubelet','containerd','crawl-kube-api'] if node.startswith('a') else ['crawl-patroni','crawl-pg-etcd','crawl-cdc-guard','pgbouncer','kafka','seaweedfs-master','seaweedfs-volume','seaweedfs-filer','seaweedfs-s3','crawl-seaweed-pg']
 units+=['crawl-log-probe','crawl-metrics-collect']
 if node=='a1':units+=['crawl-control-backup','crawl-clickhouse-backup']
 if node=='s2':units+=['crawl-pgbackrest-full','crawl-pgbackrest-diff']
 if node=='s3':units+=['clickhouse-server','crawl-log-retention']
 sources={'journal':{'type':'journald','include_units':[u+'.service' for u in units],'since_now':True},'heartbeat':{'type':'exec','mode':'scheduled','scheduled':{'exec_interval_secs':60},'command':['/usr/bin/printf','collector heartbeat']},'internal':{'type':'internal_metrics','scrape_interval_secs':15}}
 paths=['/var/log/pods/'+ns+'_*/*/*.log' for ns in ['crawl-validation','crawl-monitoring','argocd','kube-system']] if node.startswith('a') else ['/var/log/postgresql/postgresql-17-main.log','/var/log/postgresql/pgbouncer.log']
 if node=='s3':paths+=['/var/log/clickhouse-server/clickhouse-server.err.log']
 sources['files']={'type':'file','include':paths,'read_from':'beginning','ignore_older_secs':3600,'max_line_bytes':32768,'max_read_bytes':65536}
 config={'data_dir':'/var/lib/crawl-vector','sources':sources,'transforms':{'normalize':{'type':'remap','inputs':['journal','files','heartbeat'],'drop_on_error':True,'drop_on_abort':True,'source':(BASE/'normalize.vrl').read_text().replace('__NODE__',node)},'budget':{'type':'throttle','inputs':['normalize'],'threshold':200,'window_secs':10,'exclude':{'type':'vrl','source':'.Source == "heartbeat"'}}},'sinks':{'clickhouse':{'type':'clickhouse','inputs':['budget'],'endpoint':'https://10.4.4.5:8443','database':'crawler_logs','table':'events','format':'json_each_row','skip_unknown_fields':False,'date_time_best_effort':True,'compression':'gzip','auth':{'strategy':'basic','user':'crawl_logs_'+node,'password':'${CRAWL_LOG_PASSWORD}'},'tls':{'ca_file':'/etc/crawl-vector/ca.crt','verify_certificate':True,'verify_hostname':True},'batch':{'max_events':500,'max_bytes':262144,'timeout_secs':10},'buffer':{'type':'disk','max_size':536870912,'when_full':'drop_newest'},'request':{'concurrency':1,'timeout_secs':10,'retry_initial_backoff_secs':1,'retry_max_duration_secs':30}},'metrics':{'type':'prometheus_exporter','inputs':['internal'],'address':'127.0.0.1:9598'}}}
 return config
if __name__=='__main__':
 (BASE/'configs').mkdir(exist_ok=True)
 for node in NODES:(BASE/'configs'/('vector-'+node+'.yaml')).write_text(yaml.safe_dump(render(node),sort_keys=False))
 print('Rendered six bounded Vector configurations.')
