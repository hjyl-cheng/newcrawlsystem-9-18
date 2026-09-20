#!/usr/bin/env python3
"""Install node-local API load balancers first; do not yet modify Kubernetes clients."""
import argparse
import json
from pathlib import Path
import runpy
import shlex
import subprocess

ROOT=Path(__file__).resolve().parents[2]
b=runpy.run_path(str(ROOT/'ops/backup/bootstrap-pgbackrest.py'))
NODES={'a1':'10.4.4.12','a2':'10.4.4.3','a3':'10.4.4.17'}

def run(ip,command,data=None,capture=False):
    if ip==NODES['a1']:
        return subprocess.run(['bash','-c',command],input=data,text=True,check=True,capture_output=capture)
    return b['run'](ip,command,data,capture)

# Reuse the secret-safe file transport with local execution for A1.
b['put'].__globals__['run']=run
put=b['put']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('node',choices=NODES);a=p.parse_args();ip=NODES[a.node]
    run(ip,'''set -eu
sudo -n systemctl mask haproxy.service
sudo -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -qq haproxy=2.8.16-0ubuntu0.24.04.3 > /tmp/crawl-kube-api-install.log 2>&1
sudo -n install -d -m 0755 /etc/crawl-kube-api
sudo -n install -m 0644 /etc/kubernetes/pki/ca.crt /etc/crawl-kube-api/ca.crt
''',capture=True)
    put(ip,'/etc/crawl-kube-api/haproxy.cfg',(ROOT/'ops/kubernetes-ha/api-haproxy.cfg').read_text(),mode=0o644)
    put(ip,'/etc/systemd/system/crawl-kube-api.service',(ROOT/'ops/kubernetes-ha/crawl-kube-api.service').read_text(),mode=0o644)
    run(ip,'sudo -n systemctl daemon-reload && sudo -n systemctl enable --now crawl-kube-api && sudo -n systemctl is-active crawl-kube-api',capture=True)
    run(ip,'curl --fail --silent --show-error --retry 5 --retry-delay 1 --retry-all-errors --max-time 5 --cacert /etc/crawl-kube-api/ca.crt --resolve k8s-api.crawl.internal:16443:127.0.0.1 https://k8s-api.crawl.internal:16443/readyz',capture=True)
    print(a.node+': local API endpoint verified; existing client configuration unchanged.')
