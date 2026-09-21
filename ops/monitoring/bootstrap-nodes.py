#!/usr/bin/env python3
"""Install pinned node_exporter with mTLS and bounded local textfile probes."""
import argparse,hashlib,json,os,shlex,subprocess,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
PRIVATE=ROOT/'secrets/monitoring'
NODES={'a1':'10.4.4.12','a2':'10.4.4.3','a3':'10.4.4.17','s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
SSH=['ssh','-i','/home/ubuntu/.ssh/id_ed25519_crawl_infra','-o','BatchMode=yes','-o','ConnectTimeout=5']

def run(node,command,data=None):
    args=['bash','-c',command] if node=='a1' else [*SSH,'ubuntu@'+NODES[node],command]
    return subprocess.run(args,input=data,text=True,check=True,capture_output=True,timeout=90)

def put(node,path,content,owner='root',mode=0o600):
    code="""import json,os,pwd,sys,tempfile
d=json.load(sys.stdin);u=pwd.getpwnam(d['owner']);fd,tmp=tempfile.mkstemp(dir=os.path.dirname(d['path']))
os.fchmod(fd,d['mode']);os.fchown(fd,u.pw_uid,u.pw_gid)
with os.fdopen(fd,'w') as f:f.write(d['content']);f.flush();os.fsync(f.fileno())
os.replace(tmp,d['path'])
"""
    run(node,'sudo python3 -c '+shlex.quote(code),json.dumps(dict(path=path,content=content,owner=owner,mode=mode)))

def openssl(*args):subprocess.run(['openssl',*map(str,args)],check=True,capture_output=True)

def pki():
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (PRIVATE/'ca.crt').exists():
        assert not (PRIVATE/'ca.key').exists()
        openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',PRIVATE/'ca.key','-out',PRIVATE/'ca.crt','-days','1825','-subj','/CN=crawl-monitoring-ca','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    for name in [*NODES,'prometheus-client']:
        if (PRIVATE/(name+'.crt')).exists():continue
        openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',PRIVATE/(name+'.key'),'-out',PRIVATE/(name+'.csr'),'-subj','/CN='+name)
        usage='clientAuth' if name=='prometheus-client' else 'serverAuth'
        ext='basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage='+usage+'\n'
        if name in NODES:ext+='subjectAltName=IP:'+NODES[name]+',DNS:'+name+'\n'
        (PRIVATE/(name+'.ext')).write_text(ext)
        openssl('x509','-req','-in',PRIVATE/(name+'.csr'),'-CA',PRIVATE/'ca.crt','-CAkey',PRIVATE/'ca.key','-CAcreateserial','-out',PRIVATE/(name+'.crt'),'-days','730','-sha256','-extfile',PRIVATE/(name+'.ext'))

def main():
    p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args();os.umask(0o077)
    release=json.loads((ROOT/'ops/monitoring/node-exporter-release.json').read_text())
    binary=Path('/tmp/crawl-node-exporter');assert hashlib.sha256(binary.read_bytes()).hexdigest()==release['binarySHA256']
    pki()
    for name,ip in NODES.items():
        run(name,'id -u crawl-metrics >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin crawl-metrics')
        run(name,'sudo install -d -m 0755 /opt/crawlsystem/monitoring /var/lib/crawl-node-exporter/textfile && sudo install -d -m 0750 -o crawl-metrics -g crawl-metrics /etc/crawl-node-exporter')
        if name!='a1':subprocess.run(['scp','-q','-i',SSH[2],str(binary),'ubuntu@'+ip+':/tmp/crawl-node-exporter'],check=True,capture_output=True)
        run(name,'sudo install -o root -g root -m 0755 /tmp/crawl-node-exporter /usr/local/bin/crawl-node-exporter')
        for src,dst in [('ca.crt','ca.crt'),(name+'.crt','server.crt'),(name+'.key','server.key')]:put(name,'/etc/crawl-node-exporter/'+dst,(PRIVATE/src).read_text(),'crawl-metrics')
        web='tls_server_config:\n  cert_file: /etc/crawl-node-exporter/server.crt\n  key_file: /etc/crawl-node-exporter/server.key\n  client_auth_type: RequireAndVerifyClientCert\n  client_ca_file: /etc/crawl-node-exporter/ca.crt\n  min_version: TLS12\n'
        put(name,'/etc/crawl-node-exporter/web.yml',web,'crawl-metrics')
        put(name,'/opt/crawlsystem/monitoring/collect-local.py',(ROOT/'ops/monitoring/collect-local.py').read_text(),mode=0o755)
        service='''[Unit]
Description=Node and infrastructure metrics (mTLS, private network only)
After=network-online.target
Wants=network-online.target
[Service]
User=crawl-metrics
Group=crawl-metrics
ExecStart=/usr/local/bin/crawl-node-exporter --web.listen-address=IP:9100 --web.config.file=/etc/crawl-node-exporter/web.yml --collector.textfile.directory=/var/lib/crawl-node-exporter/textfile --collector.disable-defaults --collector.cpu --collector.meminfo --collector.filesystem --collector.diskstats --collector.netdev --collector.loadavg --collector.time --collector.uname --collector.stat --collector.textfile
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
MemoryMax=128M
CPUQuota=20%
[Install]
WantedBy=multi-user.target
'''.replace('IP:9100',ip+':9100')
        put(name,'/etc/systemd/system/crawl-node-exporter.service',service,mode=0o644)
        put(name,'/etc/systemd/system/crawl-metrics-collect.service','''[Unit]
Description=Read-only local infrastructure probes
[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /opt/crawlsystem/monitoring/collect-local.py
TimeoutStartSec=35
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/crawl-node-exporter/textfile
MemoryMax=128M
CPUQuota=25%
''',mode=0o644)
        put(name,'/etc/systemd/system/crawl-metrics-collect.timer','''[Unit]
Description=Refresh infrastructure metrics every 30 seconds
[Timer]
OnBootSec=10s
OnUnitActiveSec=30s
AccuracySec=1s
[Install]
WantedBy=timers.target
''',mode=0o644)
        run(name,'sudo systemctl daemon-reload && sudo systemctl enable --now crawl-node-exporter crawl-metrics-collect.timer && sudo systemctl start crawl-metrics-collect.service')
        print(name+': mTLS exporter and local collectors installed',flush=True)

if __name__=='__main__':main()
