#!/usr/bin/env python3
"""Explicit, one-node adoption; existing data required, no clone/initdb fallback."""
import argparse
import runpy
from pathlib import Path
m = runpy.run_path(str(Path(__file__).with_name('bootstrap-coordination.py')))
p = argparse.ArgumentParser()
p.add_argument('node', choices=list(m['NODES']))
a = p.parse_args()
ip = m['NODES'][a.node]
command = '''set -eu
sudo -n test -s /var/lib/postgresql/17/main/PG_VERSION
sudo -n -u postgres patroni --validate-config --ignore-listen-port /etc/crawl-patroni/patroni.yml
sudo -n systemctl stop postgresql@17-main.service
sudo -n systemctl disable postgresql@17-main.service
sudo -n systemctl mask postgresql@17-main.service
sudo -n sh -c 'echo manual > /etc/postgresql/17/main/start.conf'
sudo -n systemctl enable --now crawl-patroni.service
sudo -n systemctl is-active crawl-patroni.service
'''
m['run'](ip, command)
