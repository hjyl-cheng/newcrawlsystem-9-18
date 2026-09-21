#!/usr/bin/env python3
"""Install pinned log agents and a read-only, signed Grafana plugin artifact."""
import argparse,hashlib,importlib.util,json,os,subprocess,tarfile
import yaml
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('nodes',ROOT/'ops/monitoring/bootstrap-nodes.py');n=importlib.util.module_from_spec(s);s.loader.exec_module(n)

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true',required=True);p.parse_args()
 release=json.loads((ROOT/'ops/logging/releases.json').read_text())
 assert hashlib.sha256(Path('/tmp/crawl-vector').read_bytes()).hexdigest()==release['vector']['binarySHA256']
 plugin=Path('/tmp/crawl-clickhouse-plugin/grafana-clickhouse-datasource')
 assert (plugin/'MANIFEST.txt').exists()
 for b in plugin.glob('gpx_clickhouse*'):b.chmod(0o755)
 bundle=Path('/tmp/crawl-grafana-clickhouse.tar.gz')
 with tarfile.open(bundle,'w:gz') as tar:tar.add(plugin,arcname=plugin.name)
 service='''[Unit]
Description=Bounded infrastructure log collection to private ClickHouse HTTPS
After=network-online.target
Wants=network-online.target
[Service]
User=root
Environment=VECTOR_LOG=warn
Environment=VECTOR_THREADS=1
ExecStart=/usr/local/bin/crawl-vector --config /etc/crawl-vector/vector.yaml
Restart=always
RestartSec=5
TimeoutStopSec=45
UMask=0077
Nice=10
CPUQuota=30%
MemoryMax=256M
IOWeight=10
LimitNOFILE=4096
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
ProtectKernelModules=true
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
ReadWritePaths=/var/lib/crawl-vector
InaccessiblePaths=-/etc/crawl-patroni -/etc/crawl-pg-etcd -/etc/postgresql -/etc/pgbackrest -/etc/crawl-backup -/etc/kubernetes/pki -/etc/crawl-node-exporter -/etc/clickhouse-server
[Install]
WantedBy=multi-user.target
'''
 for node,ip in n.NODES.items():
  n.run(node,'sudo install -d -m 0700 /etc/crawl-vector /var/lib/crawl-vector')
  if node!='a1':subprocess.run(['scp','-q','-i',n.SSH[2],'/tmp/crawl-vector','ubuntu@'+ip+':/tmp/crawl-vector'],check=True,capture_output=True)
  n.run(node,'sudo install -o root -g root -m 0755 /tmp/crawl-vector /usr/local/bin/crawl-vector')
  n.put(node,'/etc/crawl-vector/ca.crt',(ROOT/'secrets/logging/ca.crt').read_text())
  password=(ROOT/'secrets/logging'/('crawl_logs_'+node+'.password')).read_text().strip()
  config=yaml.safe_load((ROOT/'ops/logging/configs'/('vector-'+node+'.yaml')).read_text())
  config['sinks']['clickhouse']['auth']['password']=password
  n.put(node,'/etc/crawl-vector/vector.yaml',yaml.safe_dump(config,sort_keys=False))
  n.run(node,'sudo rm -f /etc/crawl-vector/credentials.env')
  n.put(node,'/etc/systemd/system/crawl-vector.service',service,mode=0o644)
  n.run(node,'sudo /usr/local/bin/crawl-vector validate --skip-healthchecks /etc/crawl-vector/vector.yaml')
  n.run(node,'sudo systemctl daemon-reload && sudo systemctl enable crawl-vector && sudo systemctl restart crawl-vector')
  if node.startswith('a'):
   if node!='a1':subprocess.run(['scp','-q','-i',n.SSH[2],str(bundle),'ubuntu@'+ip+':'+str(bundle)],check=True,capture_output=True)
   dest='/opt/crawlsystem/grafana-plugins/'+release['grafana-clickhouse']['version']
   n.run(node,'sudo install -d -m 0755 '+dest+' && sudo tar -xzf '+str(bundle)+' -C '+dest+' && sudo chown -R root:root '+dest)
  if node=='s3':
   n.run(node,'sudo install -d -m 0755 /opt/crawlsystem/logging && sudo install -d -m 0700 /var/lib/crawl-log-retention')
   n.put(node,'/opt/crawlsystem/logging/retention.py',(ROOT/'ops/logging/retention.py').read_text(),mode=0o755)
   for unit in ['crawl-log-retention.service','crawl-log-retention.timer']:
    n.put(node,'/etc/systemd/system/'+unit,(ROOT/'ops/logging'/unit).read_text(),mode=0o644)
   n.run(node,'sudo systemctl daemon-reload && sudo systemctl enable --now crawl-log-retention.timer && sudo systemctl start crawl-log-retention.service')
  print(node+': log agent installed'+('; signed Grafana plugin staged' if node.startswith('a') else ''),flush=True)
if __name__=='__main__':main()
