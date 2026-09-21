#!/usr/bin/env python3
"""Encrypted etcd/config backup on A1, copied to S2. No business data dump."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import uuid

os.umask(0o077)
DEST = Path('/srv/crawlsystem/backups/control')
CONFIG = Path('/etc/crawl-backup')
SOURCE = Path('/home/ubuntu/workspace/newcrawlSystem')
IMAGE = 'registry.k8s.io/etcd@sha256:251e7e490f64859d329cd963bc879dc04acf3d7195bb52c4c50b4a07bedf37d6'
SSH = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes',
       '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'UserKnownHostsFile=/home/ubuntu/.ssh/known_hosts']
NODES = {'a1': '10.4.4.12', 'a2': '10.4.4.3', 'a3': '10.4.4.17',
         's1': '10.4.4.2', 's2': '10.4.4.8', 's3': '10.4.4.5'}
REMOTE_TAR = '''import sys,tarfile,pathlib
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz') as archive:
 for name in sys.argv[1:]:
  p=pathlib.Path('/')/name
  if p.exists(): archive.add(p,arcname=name)
'''

def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=300, **kwargs)

def container(work, command, network=False):
    args = ['ctr', '-n', 'k8s.io', 'run', '--rm']
    if network: args += ['--net-host']
    args += ['--mount', f'type=bind,src={work},dst=/backup,options=rbind:rw',
             '--mount', 'type=bind,src=/etc/kubernetes/pki/etcd,dst=/certs,options=rbind:ro',
             IMAGE, 'crawl-backup-' + uuid.uuid4().hex[:12], *command]
    return run(args, capture_output=True, text=True)

def main():
    assert os.geteuid() == 0, 'Run as root; archives contain cluster credentials'
    DEST.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(DEST / '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = datetime.now(timezone.utc).strftime('control-%Y%m%dT%H%M%SZ.tar.gpg')
        final = DEST / name
        assert not final.exists()
        with tempfile.TemporaryDirectory(prefix='.pending-', dir=DEST) as tmp:
            work = Path(tmp)
            container(work, ['etcdctl', '--endpoints=https://127.0.0.1:2379',
                '--cacert=/certs/ca.crt', '--cert=/certs/healthcheck-client.crt', '--key=/certs/healthcheck-client.key',
                'snapshot', 'save', '/backup/etcd.db'], network=True)
            status = json.loads(container(work, ['etcdutl', 'snapshot', 'status', '/backup/etcd.db', '-w', 'json']).stdout)
            pg_bin = Path('/opt/crawlsystem/backup/bin')
            pg_pki = CONFIG / 'pg-etcd-pki'
            pg_args = [str(pg_bin / 'etcdctl'), '--command-timeout=20s',
                       '--cacert=' + str(pg_pki / 'ca.crt'),
                       '--cert=' + str(pg_pki / 'admin.crt'),
                       '--key=' + str(pg_pki / 'admin.key')]
            for pg_ip in [NODES['s1'], NODES['s2'], NODES['s3']]:
                try:
                    run([*pg_args, '--endpoints=https://' + pg_ip + ':2379',
                         'snapshot', 'save', str(work / 'pg-etcd.db')], capture_output=True)
                    break
                except subprocess.CalledProcessError:
                    (work / 'pg-etcd.db.part').unlink(missing_ok=True)
            else:
                raise RuntimeError('Cannot snapshot any PG coordination endpoint')
            pg_status = json.loads(run([str(pg_bin / 'etcdutl'), 'snapshot', 'status',
                                       str(work / 'pg-etcd.db'), '-w', 'json'], capture_output=True, text=True).stdout)
            for node, ip in NODES.items():
                paths = ['etc/hosts', 'etc/systemd/system', 'etc/sysctl.d', 'etc/security/limits.d', 'etc/chrony', 'etc/crawl-node-exporter', 'opt/crawlsystem/monitoring']
                paths += (['etc/kubernetes', 'etc/containerd', 'etc/cni/net.d', 'etc/crawl-kube-api'] if node.startswith('a') else
                          ['etc/postgresql', 'etc/pgbackrest', 'etc/kafka', 'etc/seaweedfs',
                           'etc/pgbouncer', 'etc/clickhouse-server', 'usr/local/libexec',
                           'etc/crawl-pg-etcd', 'etc/crawl-patroni', 'etc/udev/rules.d',
                           'etc/modules-load.d', 'etc/modprobe.d',
                           'var/lib/postgresql/.pgpass-patroni',
                           'var/lib/postgresql/.ssh', 'var/lib/postgresql/.pgpass'])
                if node == 'a1': paths += ['etc/crawl-backup']
                if node == 's2': paths += ['var/lib/pgbackrest/.ssh']
                command = ['python3', '-c', REMOTE_TAR, *paths]
                if node != 'a1': command = SSH + ['ubuntu@' + ip, shlex.join(['sudo', '-n', *command])]
                with open(work / (node + '-config.tar.gz'), 'wb') as out:
                    run(command, stdout=out)
            with tarfile.open(work / 'operator-private.tar.gz', 'w:gz') as archive:
                for path in [SOURCE / 'secrets', SOURCE / 'SERVER_INVENTORY.local.md', Path('/home/ubuntu/.ssh'), Path('/home/ubuntu/.kube')]:
                    if path.exists(): archive.add(path, arcname=str(path).lstrip('/'))
            files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in work.iterdir() if p.is_file()}
            manifest = {'createdAt': datetime.now(timezone.utc).isoformat(), 'etcdSnapshot': status, 'pgEtcdSnapshot': pg_status,
                        'sourceRevision': subprocess.check_output(['git', '-C', str(SOURCE), '-c', 'safe.directory=' + str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip(),
                        'sha256': files, 'scope': 'etcd and configuration; not Kafka/SeaweedFS/ClickHouse data'}
            (work / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            with tarfile.open(work / 'payload.tar.gz', 'w:gz') as archive:
                for child in sorted(work.iterdir()):
                    if child.name != 'payload.tar.gz': archive.add(child, arcname=child.name)
            run(['gpg', '--homedir', str(CONFIG / 'gnupg'), '--batch', '--yes', '--pinentry-mode', 'loopback',
                 '--no-symkey-cache', '--passphrase-file', str(CONFIG / 'control-cipher-pass'),
                 '--symmetric', '--cipher-algo', 'AES256', '--compress-algo', 'none',
                 '--output', str(work / 'encrypted.gpg'), str(work / 'payload.tar.gz')])
            with open(work / 'encrypted.gpg', 'rb') as f: os.fsync(f.fileno())
            os.replace(work / 'encrypted.gpg', final)
        # Copy ciphertext only. Rename on S2 after transfer and hash verification.
        remote = str(DEST / name)
        run(SSH + ['ubuntu@' + NODES['s2'], 'sudo -n install -d -m 0700 ' + shlex.quote(str(DEST))])
        with open(final, 'rb') as stream:
            run(SSH + ['ubuntu@' + NODES['s2'], 'sudo -n sh -c ' + shlex.quote('umask 077; cat > ' + remote + '.partial')], stdin=stream)
        expected = hashlib.sha256(final.read_bytes()).hexdigest()
        checksum = subprocess.check_output(SSH + ['ubuntu@' + NODES['s2'], 'sudo -n sha256sum ' + remote + '.partial'], text=True).split()[0]
        assert checksum == expected, 'Remote ciphertext checksum mismatch'
        run(SSH + ['ubuntu@' + NODES['s2'], 'sudo -n mv ' + remote + '.partial ' + remote])
        # Only expire completed files after the fresh cross-node copy has succeeded.
        prune = '''from pathlib import Path
import re
p=Path('/srv/crawlsystem/backups/control')
files=sorted(x for x in p.iterdir() if re.fullmatch(r'control-[0-9]{8}T[0-9]{6}Z\\.tar\\.gpg',x.name))
for old in files[:-14]: old.unlink()
'''
        run(['python3', '-c', prune])
        run(SSH + ['ubuntu@' + NODES['s2'], 'sudo -n python3 -c ' + shlex.quote(prune)])
        result = {'status': 'PASSED', 'file': name, 'sha256': expected, 'bytes': final.stat().st_size,
                  'copies': ['a1', 's2'], 'etcdSnapshot': status, 'pgEtcdSnapshot': pg_status}
        (DEST / 'last-success.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))

if __name__ == '__main__': main()
