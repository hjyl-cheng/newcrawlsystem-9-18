#!/usr/bin/env python3
"""After metadata migration, add two filers and authenticated S3 on all S nodes."""
import json
import os
from pathlib import Path
import runpy
import secrets
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
h=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
run,put=h['run'],h['put']
NODES={'s1':'10.4.4.2','s2':'10.4.4.8','s3':'10.4.4.5'}
MASTERS=','.join(ip+':9333' for ip in NODES.values())

def ready(ip):
    for _ in range(120):
        try:
            req=urllib.request.Request('http://'+ip+':8888/?pretty=y',headers={'Accept':'application/json'})
            result=json.load(urllib.request.urlopen(req,timeout=3))
            if result.get('Entries'):return
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('Filer not ready on '+ip)

if __name__=='__main__':
    assert json.loads((ROOT/'ops/checks/2026-09-21-seaweed-metadata-migration.json').read_text())['status']=='PASSED'
    os.umask(0o077)
    credentials=ROOT/'secrets/seaweedfs/s3-credentials.json'
    if not credentials.exists():
        credentials.write_text(json.dumps({name:{'accessKey':secrets.token_hex(12),'secretKey':secrets.token_hex(32)} for name in ['operator','worker']},indent=2)+'\n')
    credentials.chmod(0o600)
    keys=json.loads(credentials.read_text())
    config={'identities':[
        {'name':'crawl-object-operator','credentials':[keys['operator']], 'actions':['Admin','Read','Write','List','Tagging']},
        {'name':'crawl-validation-worker','credentials':[keys['worker']], 'actions':[action+':crawl-validation-objects' for action in ['Read','Write','List','Tagging']]},
    ]}
    for name,ip in NODES.items():
        # Also apply the startup-safe health-check state to each local DB selector.
        put(ip,'/etc/seaweedfs/metadata-initial.state',(ROOT/'ops/seaweedfs/metadata-initial.state').read_text(),mode=0o644)
        put(ip,'/etc/seaweedfs/metadata-haproxy.cfg',(ROOT/'ops/seaweedfs/metadata-haproxy.cfg').read_text(),mode=0o644)
        run(ip,'sudo /usr/sbin/haproxy -c -f /etc/seaweedfs/metadata-haproxy.cfg && sudo systemctl restart crawl-seaweed-pg',capture=True)
        if name!='s3':
            run(ip,'sudo install -m 0600 -o seaweedfs -g seaweedfs /etc/seaweedfs/filer-postgres.pending.toml /etc/seaweedfs/filer.toml')
            cmd=(f'/opt/seaweedfs/weed filer -ip={ip} -ip.bind={ip} -port=8888 -master={MASTERS} '
                 '-filerGroup=crawl-objects -defaultReplicaPlacement=001 -concurrentFileUploadLimit=4 -concurrentUploadLimitMB=16 -maxMB=4')
            service=f'''[Unit]
Description=SeaweedFS shared-PG Filer
After=network-online.target crawl-seaweed-pg.service
Wants=network-online.target crawl-seaweed-pg.service
[Service]
User=seaweedfs
Group=seaweedfs
WorkingDirectory=/etc/seaweedfs
ExecStart={cmd}
Restart=on-failure
RestartSec=3
LimitNOFILE=16384
[Install]
WantedBy=multi-user.target
'''
            put(ip,'/etc/systemd/system/seaweedfs-filer.service',service,mode=0o644)
            run(ip,'sudo systemctl daemon-reload && sudo systemctl enable --now seaweedfs-filer',capture=True)
        ready(ip)
        print(name+': Filer reads shared metadata')
    # A real metadata lookup is part of gateway health, not only a TCP connect.
    urllib.request.urlopen(urllib.request.Request('http://10.4.4.5:8888/crawlsystem-infra/ready',method='PUT',data=b'ready-v1\n'),timeout=10).close()
    for name,ip in NODES.items():
        put(ip,'/etc/seaweedfs/s3-credentials.json',json.dumps(config,indent=2)+'\n','seaweedfs',0o600)
        filers=','.join(host+':8888' for host in [ip,*[x for x in NODES.values() if x!=ip]])
        cmd=(f'/opt/seaweedfs/weed s3 -ip={ip} -ip.bind={ip} -port=8333 -filer={filers} '
             '-config=/etc/seaweedfs/s3-credentials.json -iam=false -autoCreateBucket=false '
             '-allowDeleteBucketNotEmpty=false -concurrentFileUploadLimit=4 -concurrentUploadLimitMB=16')
        service=f'''[Unit]
Description=SeaweedFS authenticated S3 Gateway
After=network-online.target seaweedfs-filer.service
Wants=network-online.target
[Service]
User=seaweedfs
Group=seaweedfs
WorkingDirectory=/etc/seaweedfs
ExecStart={cmd}
Restart=on-failure
RestartSec=3
LimitNOFILE=16384
[Install]
WantedBy=multi-user.target
'''
        put(ip,'/etc/systemd/system/seaweedfs-s3.service',service,mode=0o644)
        run(ip,'sudo systemctl daemon-reload && sudo systemctl enable seaweedfs-s3 && sudo systemctl restart seaweedfs-s3',capture=True)
        print(name+': authenticated S3 gateway configured')
