#!/usr/bin/env python3
"""Decrypt the newest archive and boot its etcd snapshot on isolated loopback ports."""
import hashlib
import json
import os
import re
from pathlib import Path
import runpy
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid

os.umask(0o077)
m = runpy.run_path(str(Path(__file__).with_name('backup-control.py')))
root = m['DEST']
assert os.geteuid() == 0
archive = root / json.loads((root / 'last-success.json').read_text())['file']
for port in (12379, 12380):
    with socket.socket() as check: check.bind(('127.0.0.1', port))

with tempfile.TemporaryDirectory(prefix='control-restore-', dir=root.parent) as tmp:
    work = Path(tmp)
    subprocess.run(['gpg', '--homedir', '/etc/crawl-backup/gnupg', '--batch', '--yes',
        '--pinentry-mode', 'loopback', '--no-symkey-cache', '--passphrase-file', '/etc/crawl-backup/control-cipher-pass',
        '--output', str(work / 'payload.tar.gz'), '--decrypt', str(archive)], check=True, timeout=60)
    with tarfile.open(work / 'payload.tar.gz') as stream: stream.extractall(work, filter='data')
    manifest = json.loads((work / 'manifest.json').read_text())
    for name, sha in manifest['sha256'].items():
        assert Path(name).name == name
        assert hashlib.sha256((work / name).read_bytes()).hexdigest() == sha, name + ' checksum mismatch'
    m['container'](work, ['etcdutl', 'snapshot', 'restore', '/backup/etcd.db',
        '--data-dir=/backup/restored-etcd', '--name=backup-verify',
        '--initial-cluster=backup-verify=http://127.0.0.1:12380',
        '--initial-advertise-peer-urls=http://127.0.0.1:12380',
        '--initial-cluster-token=crawl-offline-verification'])
    # PG coordination is a separate authenticated quorum; restore its snapshot independently.
    pg_private = m['SOURCE'] / 'secrets/postgresql-ha'
    pg_bin = Path('/opt/crawlsystem/backup/bin')
    pg_pki = work / 'pg-pki'
    pg_pki.mkdir(mode=0o700)
    with tarfile.open(work / 'operator-private.tar.gz') as operator:
        for file in ['ca.crt', 's1.crt', 's1.key', 'admin.crt', 'admin.key']:
            member = str(pg_private / 'pki' / file).lstrip('/')
            stream = operator.extractfile(member)
            assert stream is not None
            (pg_pki / file).write_bytes(stream.read())
    for port in (12479,12480):
        with socket.socket() as check: check.bind(('127.0.0.1', port))
    subprocess.run([str(pg_bin / 'etcdutl'), 'snapshot', 'restore', str(work / 'pg-etcd.db'),
        '--data-dir=' + str(work / 'restored-pg-etcd'), '--name=pg-backup-verify',
        '--initial-cluster=pg-backup-verify=http://127.0.0.1:12480',
        '--initial-advertise-peer-urls=http://127.0.0.1:12480'], check=True, capture_output=True)
    pg_ctl = [str(pg_bin / 'etcdctl'), '--endpoints=https://127.0.0.1:12479', '--command-timeout=2s',
        '--cacert=' + str(pg_pki / 'ca.crt'), '--cert=' + str(pg_pki / 'admin.crt'), '--key=' + str(pg_pki / 'admin.key')]
    with open(work / 'pg-etcd.log', 'w') as log:
        pg_proc = subprocess.Popen([str(pg_bin / 'etcd'), '--name=pg-backup-verify',
            '--data-dir=' + str(work / 'restored-pg-etcd'),
            '--listen-client-urls=https://127.0.0.1:12479', '--advertise-client-urls=https://127.0.0.1:12479',
            '--listen-peer-urls=http://127.0.0.1:12480', '--initial-advertise-peer-urls=http://127.0.0.1:12480',
            '--initial-cluster=pg-backup-verify=http://127.0.0.1:12480',
            '--client-cert-auth', '--trusted-ca-file=' + str(pg_pki / 'ca.crt'),
            '--cert-file=' + str(pg_pki / 's1.crt'), '--key-file=' + str(pg_pki / 's1.key')], stdout=log, stderr=log)
        try:
            for _ in range(20):
                response = subprocess.run([*pg_ctl, 'get', '/service/crawl-pg/', '--prefix', '--count-only', '-w', 'fields'], capture_output=True,text=True)
                if response.returncode == 0: break
                if pg_proc.poll() is not None: raise RuntimeError('Restored PG etcd exited')
                time.sleep(1)
            else: raise RuntimeError('Restored PG etcd unavailable')
            pg_count = int(re.search(r'Count\"?\s*:\s*(\d+)', response.stdout)[1])
            assert pg_count >= 5
            pg_config = json.loads(subprocess.check_output([*pg_ctl, 'get', '/service/crawl-pg/config', '--print-value-only'], text=True))
            assert pg_config['synchronous_mode_strict'] is True
            pg_auth = json.loads(subprocess.check_output([*pg_ctl, 'auth', 'status', '-w', 'json'], text=True))
            assert pg_auth['enabled'] is True
        finally:
            pg_proc.terminate()
            try: pg_proc.wait(timeout=10)
            except subprocess.TimeoutExpired: pg_proc.kill(); pg_proc.wait(timeout=5)
    task = 'crawl-etcd-restore-' + uuid.uuid4().hex[:12]
    args = ['ctr', '-n', 'k8s.io', 'run', '--rm', '--net-host', '--mount',
        f'type=bind,src={work},dst=/backup,options=rbind:rw', m['IMAGE'], task, 'etcd',
        '--data-dir=/backup/restored-etcd', '--name=backup-verify',
        '--listen-client-urls=http://127.0.0.1:12379', '--advertise-client-urls=http://127.0.0.1:12379',
        '--listen-peer-urls=http://127.0.0.1:12380', '--initial-advertise-peer-urls=http://127.0.0.1:12380',
        '--initial-cluster=backup-verify=http://127.0.0.1:12380',
        '--initial-cluster-token=crawl-offline-verification']
    with open(work / 'etcd.log', 'w') as log:
        proc = subprocess.Popen(args, stdout=log, stderr=log)
        try:
            for _ in range(30):
                try:
                    m['container'](work, ['etcdctl', '--endpoints=http://127.0.0.1:12379',
                        '--dial-timeout=1s', '--command-timeout=2s', 'endpoint', 'health'], network=True)
                    break
                except subprocess.CalledProcessError:
                    if proc.poll() is not None: raise RuntimeError('Restored etcd exited')
                    time.sleep(1)
            else: raise RuntimeError('Restored etcd did not become healthy')
            counts = {}
            for prefix in ['/registry/', '/registry/secrets/', '/registry/deployments/']:
                try:
                    response = m['container'](work, ['etcdctl', '--endpoints=http://127.0.0.1:12379',
                        'get', prefix, '--prefix', '--count-only', '--write-out=fields'], network=True)
                except subprocess.CalledProcessError as error:
                    raise RuntimeError(error.stderr) from None
                match = re.search(r'"?Count"?\s*:\s*(\d+)', response.stdout)
                assert match, 'Missing count in etcdctl response'
                counts[prefix] = int(match[1])
                assert counts[prefix] > 0
            print(json.dumps(dict(status='PASSED', archive=archive.name,
                filesVerified=len(manifest['sha256']), restoredKeyCounts=counts, pgCoordinationKeys=pg_count, pgAuthRestored=True)))
        finally:
            subprocess.run(['ctr', '-n', 'k8s.io', 'tasks', 'kill', '--signal', 'SIGTERM', task], check=False)
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(['ctr', '-n', 'k8s.io', 'tasks', 'kill', '--signal', 'SIGKILL', task], check=False)
                proc.wait(timeout=10)
