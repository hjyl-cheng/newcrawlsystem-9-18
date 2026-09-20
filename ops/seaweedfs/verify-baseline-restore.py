#!/usr/bin/env python3
"""Run on S3 as root, JSON input: {file, expected:[{path,bytes,sha256}]}.

Decrypt the baseline into new directories and start an isolated loopback-only
Master/Volume/Filer. No production data directory or master endpoint is used.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid

def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=45, capture_output=True, **kwargs)

def main():
    assert os.geteuid() == 0
    os.umask(0o077)
    spec = json.load(sys.stdin)
    assert re.fullmatch(r'seaweed-baseline-\d{8}T\d{6}Z\.tar\.gpg', spec['file'])
    archive = Path('/srv/crawlsystem/backups/seaweedfs-baseline') / spec['file']
    work = Path('/srv/crawlsystem') / ('restore-check-sw-' + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    units = ['crawl-sw-restore-' + work.name[-12:] + '-' + suffix for suffix in ['master', 'volume', 'filer']]
    timer = 'crawl-sw-restore-cleanup-' + work.name[-12:]
    started = False
    evidence = {'status': 'RUNNING', 'archive': archive.name, 'objects': [],
                'isolation': 'new directories, loopback-only master 19334 / volume 18081 / filer 18889; separate grpc ports'}
    try:
        payload = work / 'payload.tar'
        run(['gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase-file', '/etc/crawl-backup/control-cipher-pass',
             '--output', str(payload), '--decrypt', str(archive)])
        with tarfile.open(payload) as tar:
            tar.extractall(work, filter='data')
        manifest = json.loads((work / 'manifest.json').read_text())
        for name, digest in manifest['sha256'].items():
            assert hashlib.sha256((work / name).read_bytes()).hexdigest() == digest
            folder = work / name.removesuffix('.tar.gz')
            folder.mkdir()
            with tarfile.open(work / name) as tar:
                tar.extractall(folder, filter='data')
        config = work / 'config'
        config.mkdir()
        (config / 'filer.toml').write_text('[leveldb2]\nenabled = true\ndir = "' + str(work / 's3/filer/filerldb2') + '"\n')
        (work / 'master').mkdir()
        user = pwd.getpwnam('seaweedfs')
        for directory, subdirs, files in os.walk(work):
            os.chown(directory, user.pw_uid, user.pw_gid)
            for name in files:
                os.chown(Path(directory) / name, user.pw_uid, user.pw_gid)
        run(['systemd-run', '--unit=' + timer, '--on-active=3m', '/usr/bin/systemctl', 'stop', *reversed(units)])
        started = True
        common = ['/opt/seaweedfs/weed']
        commands = [
            common + ['master', '-ip=127.0.0.1', '-ip.bind=127.0.0.1', '-port=19334', '-port.grpc=29334',
                      '-peers=none', '-mdir=' + str(work / 'master')],
            common + ['volume', '-ip=127.0.0.1', '-ip.bind=127.0.0.1', '-port=18081', '-port.grpc=28081',
                      '-master=127.0.0.1:19334', '-max=8,8,8', '-dir=' + ','.join(str(work / node / 'volume') for node in ['s1','s2','s3'])],
            common + ['filer', '-ip=127.0.0.1', '-ip.bind=127.0.0.1', '-port=18889', '-port.grpc=28889',
                      '-master=127.0.0.1:19334', '-filerGroup=restore-check', '-defaultStoreDir=' + str(work / 's3/filer')],
        ]
        for unit, command in zip(units, commands):
            run(['systemd-run', '--unit=' + unit, '--property=User=seaweedfs', '--property=Group=seaweedfs',
                 '--property=WorkingDirectory=' + str(config), '--property=TimeoutStopSec=20', *command])
        for expected in spec['expected']:
            assert expected['path'].startswith('/topics/.system/log/2026-09-18/')
            for _ in range(45):
                try:
                    data = urllib.request.urlopen('http://127.0.0.1:18889' + expected['path'], timeout=2).read()
                    break
                except Exception:
                    time.sleep(.5)
            else:
                raise RuntimeError('Restored filer could not serve original file')
            assert len(data) == expected['bytes']
            assert hashlib.sha256(data).hexdigest() == expected['sha256']
            evidence['objects'].append(expected)
        # Directory lookup and volume locations must resolve inside the isolated deployment.
        topology = json.load(urllib.request.urlopen('http://127.0.0.1:19334/dir/status', timeout=3))
        for dc in topology['Topology']['DataCenters']:
            for rack in dc['Racks']:
                assert all(n['Url'] == '127.0.0.1:18081' for n in rack['DataNodes'])
        evidence['topology'] = topology
        evidence['status'] = 'PASSED'
    finally:
        if started:
            run(['systemctl', 'stop', *reversed(units)])
            run(['systemctl', 'stop', timer + '.timer'])
        shutil.rmtree(work)
    evidence['cleanup'] = 'isolated services stopped, timer removed, temporary directories removed'
    print(json.dumps(evidence))

if __name__ == '__main__':
    main()
