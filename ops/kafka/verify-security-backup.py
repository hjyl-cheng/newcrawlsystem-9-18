#!/usr/bin/env python3
"""Decrypt the latest control archive and verify staged Kafka PKI/config coverage.

Run with sudo. No key material is printed or written outside a private temp directory.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BACKUP = Path('/srv/crawlsystem/backups/control')


def main():
    assert os.geteuid() == 0
    os.umask(0o077)
    info = json.loads((BACKUP/'last-success.json').read_text())
    archive = BACKUP/info['file']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == info['sha256']
    remote = subprocess.check_output(['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra',
        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile=/home/ubuntu/.ssh/known_hosts', 'ubuntu@10.4.4.8',
        'sudo sha256sum '+str(archive)], text=True, timeout=30).split()[0]
    assert remote == info['sha256']
    with tempfile.TemporaryDirectory(prefix='kafka-backup-check-', dir=BACKUP) as temp:
        payload = Path(temp)/'payload.tar.gz'
        subprocess.run(['gpg', '--homedir', '/etc/crawl-backup/gnupg', '--batch', '--yes',
            '--pinentry-mode', 'loopback', '--no-symkey-cache', '--passphrase-file',
            '/etc/crawl-backup/control-cipher-pass', '--output', str(payload), '--decrypt',
            str(archive)], check=True, capture_output=True)
        with tarfile.open(payload) as outer:
            manifest = json.load(outer.extractfile('manifest.json'))
            for name, digest in manifest['sha256'].items():
                assert hashlib.sha256(outer.extractfile(name).read()).hexdigest() == digest
            for node in ['s1', 's2', 's3']:
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile(node+'-config.tar.gz').read())) as host:
                    conf = host.extractfile('etc/kafka/server.properties').read().decode()
                    assert 'listener.name.secure.ssl.client.auth=required' in conf
                    assert 'SECURE://' in conf and ':9094' in conf
                    assert 'inter.broker.listener.name=PLAINTEXT' in conf
                    for name, local in [('ca.crt','ca.crt'), ('node.crt',node+'.crt'), ('node.key',node+'.key')]:
                        member = host.getmember('etc/kafka/tls/'+name)
                        assert member.mode == 0o600
                        assert host.extractfile(member).read() == (ROOT/'secrets/kafka/pki'/local).read_bytes()
                    assert host.extractfile('etc/kafka/tls/node.pem').read() == (ROOT/'secrets/kafka/pki'/(node+'.key')).read_bytes()+(ROOT/'secrets/kafka/pki'/(node+'.crt')).read_bytes()
                    assert not any(name.endswith('/ca.key') for name in host.getnames() if name.startswith('etc/kafka/'))
            with tarfile.open(fileobj=io.BytesIO(outer.extractfile('operator-private.tar.gz').read())) as private:
                for name in ['ca.key','ca.crt','operator.key','operator.crt']:
                    path = ROOT/'secrets/kafka/pki'/name
                    assert private.extractfile(str(path).lstrip('/')).read() == path.read_bytes()
    result = {'control': info, 'sourceRevision': manifest['sourceRevision'], 'verification': {
        'manifestHashes': 'all matched', 'crossHostCiphertext': 'A1/S2 matched',
        'kafkaConfigs': 'three SECURE listeners; old transports explicitly retained',
        'nodeKeysCertificates': 'all match, protected permissions',
        'operatorCAAndKey': 'present and matched; CA private key absent from host Kafka directories'},
        'scope': 'Configuration/identity recoverability only, not live Kafka messages or joint PG/Kafka disaster recovery'}
    # Print only after every validation succeeds; the caller saves this non-secret result.
    print(json.dumps(result, indent=2))


if __name__ == '__main__':main()
