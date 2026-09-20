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
                filesVerified=len(manifest['sha256']), restoredKeyCounts=counts)))
        finally:
            subprocess.run(['ctr', '-n', 'k8s.io', 'tasks', 'kill', '--signal', 'SIGTERM', task], check=False)
            try: proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                subprocess.run(['ctr', '-n', 'k8s.io', 'tasks', 'kill', '--signal', 'SIGKILL', task], check=False)
                proc.wait(timeout=10)
