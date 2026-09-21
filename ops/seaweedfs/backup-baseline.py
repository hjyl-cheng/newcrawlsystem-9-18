#!/usr/bin/env python3
"""Bounded maintenance backup of the current small SeaweedFS deployment.

Stops the single S3/filer entry and each volume in turn. Not an online production
backup service. Uses protected local staging and the existing control-backup key.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
SSH = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
NODES = {'s1': '10.4.4.2', 's2': '10.4.4.8', 's3': '10.4.4.5'}

def remote(ip, command, **kwargs):
    return subprocess.run(SSH + ['ubuntu@' + ip, command], check=True, timeout=60, **kwargs)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', required=True)
    parser.parse_args()
    # This legacy procedure cannot capture the shared PostgreSQL metadata.
    for ip in NODES.values():
        remote(ip, 'sudo test ! -f /etc/seaweedfs/filer.toml', capture_output=True)
    # This baseline procedure is intentionally limited to the tiny validation
    # deployment; do not let automatic recovery resume writes during a large copy.
    for ip in NODES.values():
        size = int(remote(ip, 'sudo du -sk /srv/crawlsystem/seaweedfs', capture_output=True, text=True).stdout.split()[0])
        assert size < 131072, 'Baseline procedure is limited to <128 MiB per node; use an online backup design for larger stores'
    os.umask(0o077)
    private = ROOT / 'secrets/seaweedfs'
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    unit = 'crawl-seaweed-recover-' + uuid.uuid4().hex[:12]
    encrypted = private / ('seaweed-baseline-' + stamp + '.tar.gpg')
    manifest = {'createdAt': stamp, 'scope': 'Cold volume files and S3 local filer metadata; no live master raft snapshot',
                'sourceRevision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'sha256': {}}
    armed = []
    with tempfile.TemporaryDirectory(prefix='baseline-', dir=private) as tmp:
        work = Path(tmp)
        try:
            for name, ip in NODES.items():
                services = 'seaweedfs-volume' + (' seaweedfs-filer seaweedfs-s3' if name == 's3' else '')
                remote(ip, f'sudo systemd-run --unit={unit} --on-active=5m /usr/bin/systemctl start {services}', capture_output=True)
                armed.append((name, ip, services))
            remote(NODES['s3'], 'sudo systemctl stop seaweedfs-s3 seaweedfs-filer')
            for name, ip in NODES.items():
                remote(ip, 'sudo systemctl stop seaweedfs-volume')
                try:
                    path = work / (name + '.tar.gz')
                    command = ('sudo tar -czf - -C /srv/crawlsystem/seaweedfs volume filer '
                               '-C / etc/seaweedfs etc/systemd/system/seaweedfs-master.service '
                               'etc/systemd/system/seaweedfs-volume.service')
                    if name == 's3':
                        command += ' etc/systemd/system/seaweedfs-filer.service etc/systemd/system/seaweedfs-s3.service'
                    with path.open('wb') as out:
                        remote(ip, command, stdout=out)
                    manifest['sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                finally:
                    remote(ip, 'sudo systemctl start seaweedfs-volume')
        finally:
            for name, ip, services in reversed(armed):
                # Keep the independent timer if explicit recovery fails.
                remote(ip, f'sudo systemctl start {services}')
                remote(ip, f'systemctl is-active {services}', capture_output=True)
                remote(ip, f'sudo systemctl stop {unit}.timer')
        (work / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        payload = work / 'payload.tar'
        with tarfile.open(payload, 'w') as archive:
            for p in sorted(work.iterdir()):
                if p != payload:
                    archive.add(p, arcname=p.name)
        subprocess.run(['sudo', 'gpg', '--batch', '--yes', '--pinentry-mode', 'loopback',
                        '--passphrase-file', '/etc/crawl-backup/control-cipher-pass',
                        '--symmetric', '--cipher-algo', 'AES256', '--output', str(encrypted), str(payload)], check=True)
        subprocess.run(['sudo', 'chown', f'{os.getuid()}:{os.getgid()}', str(encrypted)], check=True)
        # Verify decryption and every member checksum without restoring into live paths.
        decoded = work / 'decoded.tar'
        subprocess.run(['sudo', 'gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase-file',
                        '/etc/crawl-backup/control-cipher-pass', '--output', str(decoded), '--decrypt', str(encrypted)],
                       check=True, capture_output=True)
        subprocess.run(['sudo', 'chown', f'{os.getuid()}:{os.getgid()}', str(decoded)], check=True)
        with tarfile.open(decoded) as archive:
            for name, digest in manifest['sha256'].items():
                assert hashlib.sha256(archive.extractfile(name).read()).hexdigest() == digest
        destination = '/srv/crawlsystem/backups/seaweedfs-baseline/' + encrypted.name
        for name in ['s2', 's3']:
            ip = NODES[name]
            remote(ip, 'sudo install -d -m 0700 /srv/crawlsystem/backups/seaweedfs-baseline')
            with encrypted.open('rb') as source:
                remote(ip, f'sudo sh -c "umask 077; cat > {destination}"', stdin=source)
            digest = remote(ip, f'sudo sha256sum {destination}', capture_output=True, text=True).stdout.split()[0]
            assert digest == hashlib.sha256(encrypted.read_bytes()).hexdigest()
    result = {'status': 'PASSED', 'file': encrypted.name, 'bytes': encrypted.stat().st_size,
              'sha256': hashlib.sha256(encrypted.read_bytes()).hexdigest(), 'copies': ['a1-protected-staging', 's2', 's3'],
              'decryptionAndArchiveChecksums': 'PASSED', 'independentServiceRestore': 'NOT_YET_TESTED', 'manifest': manifest}
    (ROOT / 'ops/checks' / ('seaweed-baseline-' + stamp + '.json')).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))

if __name__ == '__main__':
    main()
