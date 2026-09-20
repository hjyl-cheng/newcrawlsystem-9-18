#!/usr/bin/env python3
"""Enable prefix-scoped DCS authorization; secrets stay in memory and protected files."""
import base64
import json
from pathlib import Path
import runpy
import ssl
import subprocess
import urllib.request

m = runpy.run_path(str(Path(__file__).with_name('bootstrap-coordination.py')))
pki, private = m['PKI'], m['PRIVATE']
endpoints = ','.join('https://' + ip + ':2379' for ip in m['NODES'].values())
args = [str(private / 'artifacts/etcdctl'), '--endpoints=' + endpoints,
        '--cacert=' + str(pki / 'ca.crt'), '--cert=' + str(pki / 'admin.crt'), '--key=' + str(pki / 'admin.key')]

def ctl(*command):
    return subprocess.check_output([*args, *command], text=True, timeout=20)

if __name__ == '__main__':
    status = json.loads(ctl('auth', 'status', '-w', 'json'))
    if not status.get('enabled'):
        users = ctl('user', 'list').splitlines()
        if 'root' not in users: ctl('user', 'add', 'root', '--no-password')
        ctl('user', 'grant-role', 'root', 'root')
        if 'patroni' not in users:
            ctx = ssl.create_default_context(cafile=str(pki / 'ca.crt'))
            ctx.load_cert_chain(str(pki / 'admin.crt'), str(pki / 'admin.key'))
            data = json.dumps({'name': 'patroni', 'password': (private / 'dcs-password').read_text().strip()}).encode()
            req = urllib.request.Request('https://10.4.4.2:2379/v3/auth/user/add', data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as response: json.load(response)
        if 'patroni' not in ctl('role', 'list').splitlines(): ctl('role', 'add', 'patroni')
        ctl('role', 'grant-permission', 'patroni', 'readwrite', '/service/crawl-pg/', '--prefix')
        ctl('user', 'grant-role', 'patroni', 'patroni')
        ctl('auth', 'enable')
    assert json.loads(ctl('auth', 'status', '-w', 'json'))['enabled']
    print(ctl('endpoint', 'health').strip())
    print(ctl('endpoint', 'status', '-w', 'table').strip())
    print('DCS authorization enabled; Patroni role restricted to /service/crawl-pg/.')
