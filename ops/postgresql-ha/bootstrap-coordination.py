#!/usr/bin/env python3
"""Install an independent TLS etcd quorum for PG; never adopt or restart PostgreSQL."""
import hashlib
import json
from pathlib import Path
import runpy
import secrets
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[2]
m = runpy.run_path(str(ROOT / 'ops/backup/bootstrap-pgbackrest.py'))
run, put = m['run'], m['put']
NODES = {'s1': '10.4.4.2', 's2': '10.4.4.8', 's3': '10.4.4.5'}
PRIVATE = ROOT / 'secrets/postgresql-ha'
PKI = PRIVATE / 'pki'
HASHES = {
    'etcd': '3e756e949f6d6e08ac01da34b1a6d339776a572a1716cc6a5d1bea9922b6b433',
    'etcdctl': '4a9641049ac312650efa088697918c82ec89c37ccdc0bbd150592ff9c6aa8509',
    'etcdutl': '94252219fc9e5d79dd4fd889269d73393fc5f1ce51e099de326de2b2972c09b9',
}

def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

def certificate(name, cn, sans=None):
    cert, key = PKI / (name + '.crt'), PKI / (name + '.key')
    if cert.exists():
        assert key.exists()
        openssl('verify', '-CAfile', PKI / 'ca.crt', cert)
        return
    assert not key.exists(), 'Incomplete credential set requires inspection'
    openssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256', '-out', key)
    key.chmod(0o600)
    csr, ext = PKI / (name + '.csr'), PKI / (name + '.ext')
    openssl('req', '-new', '-key', key, '-subj', ('/CN=' + cn if cn else '/OU=patroni-gateway'), '-out', csr)
    ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=' +
                   ('serverAuth,clientAuth' if sans else 'clientAuth') + '\n' +
                   ('subjectAltName=' + sans + '\n' if sans else ''))
    openssl('x509', '-req', '-in', csr, '-CA', PKI / 'ca.crt', '-CAkey', PKI / 'ca.key',
            '-set_serial', str(secrets.randbits(120)), '-days', '365', '-sha256', '-extfile', ext, '-out', cert)

def prepare_pki():
    PKI.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (PKI / 'ca.crt').exists():
        assert not (PKI / 'ca.key').exists(), 'Incomplete CA requires inspection'
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-keyout', PKI / 'ca.key', '-out', PKI / 'ca.crt', '-days', '1825', '-sha256',
                '-subj', '/CN=crawl-postgresql-coordination-ca',
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        (PKI / 'ca.key').chmod(0o600)
    for name, ip in NODES.items(): certificate(name, 'etcd-' + name, f'IP:{ip},IP:127.0.0.1,DNS:{name},DNS:localhost')
    certificate('admin', 'root')
    certificate('patroni', 'patroni')
    # etcd's HTTP gateway rejects nonempty certificate CN even with password auth.
    certificate('gateway', None)
    password = PRIVATE / 'dcs-password'
    if not password.exists(): password.write_text(secrets.token_hex(32) + '\n')
    password.chmod(0o600)

def install():
    prepare_pki()
    binaries = {}
    for name, sha in HASHES.items():
        data = (PRIVATE / 'artifacts' / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha, 'Pinned binary mismatch: ' + name
        binaries[name] = data
    cluster = ','.join(f'{name}=https://{ip}:2380' for name, ip in NODES.items())
    for name, ip in NODES.items():
        run(ip, 'id crawl-etcd >/dev/null 2>&1 || sudo -n useradd --system --home-dir /var/lib/crawl-pg-etcd --shell /usr/sbin/nologin crawl-etcd')
        run(ip, 'sudo -n install -d -m 0755 /opt/crawl-pg-ha/bin && sudo -n install -d -m 0700 -o crawl-etcd -g crawl-etcd /etc/crawl-pg-etcd /etc/crawl-pg-etcd/pki /srv/crawlsystem/pg-ha/etcd')
        for tool, data in binaries.items():
            path = '/opt/crawl-pg-ha/bin/' + tool
            command = 'umask 022; cat > ' + path + '.new && chmod 0755 ' + path + '.new && mv ' + path + '.new ' + path
            subprocess.run(m['SSH'] + ['ubuntu@' + ip, 'sudo -n sh -c ' + shlex.quote(command)], input=data, check=True)
        for source, dest in [('ca.crt', 'ca.crt'), (name + '.crt', 'server.crt'), (name + '.key', 'server.key')]:
            put(ip, '/etc/crawl-pg-etcd/pki/' + dest, (PKI / source).read_text(), 'crawl-etcd')
        config = {
            'name': name, 'data-dir': '/srv/crawlsystem/pg-ha/etcd',
            'listen-client-urls': f'https://{ip}:2379,https://127.0.0.1:2379',
            'advertise-client-urls': f'https://{ip}:2379',
            'listen-peer-urls': f'https://{ip}:2380', 'initial-advertise-peer-urls': f'https://{ip}:2380',
            'initial-cluster': cluster, 'initial-cluster-state': 'new',
            'initial-cluster-token': 'crawl-pg-coordination-20260920-v1',
            'client-transport-security': {'cert-file': '/etc/crawl-pg-etcd/pki/server.crt',
                'key-file': '/etc/crawl-pg-etcd/pki/server.key', 'client-cert-auth': True,
                'trusted-ca-file': '/etc/crawl-pg-etcd/pki/ca.crt'},
            'peer-transport-security': {'cert-file': '/etc/crawl-pg-etcd/pki/server.crt',
                'key-file': '/etc/crawl-pg-etcd/pki/server.key', 'client-cert-auth': True,
                'trusted-ca-file': '/etc/crawl-pg-etcd/pki/ca.crt'},
            'auto-compaction-mode': 'periodic', 'auto-compaction-retention': '1h',
            'quota-backend-bytes': 134217728, 'snapshot-count': 10000,
            'logger': 'zap', 'log-level': 'warn',
        }
        put(ip, '/etc/crawl-pg-etcd/etcd.json', json.dumps(config, indent=2), 'crawl-etcd')
        put(ip, '/etc/systemd/system/crawl-pg-etcd.service', (ROOT / 'ops/postgresql-ha/crawl-pg-etcd.service').read_text(), mode=0o644)
        run(ip, 'sudo -n systemctl daemon-reload && sudo -n systemctl enable crawl-pg-etcd.service && sudo -n systemctl start --no-block crawl-pg-etcd.service')
    print('Independent PG coordination quorum configured. Existing PostgreSQL not modified.')

if __name__ == '__main__': install()
