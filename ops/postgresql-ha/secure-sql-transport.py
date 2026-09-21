#!/usr/bin/env python3
"""Stage SQL TLS without restarting PostgreSQL or changing the CDC barrier.

Explicit phases keep server compatibility, client migration, and enforcement separate.
Private material and original configuration are saved only under ignored secrets/.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import struct
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
nodes = runpy.run_path(str(ROOT / 'ops/monitoring/bootstrap-nodes.py'))
pg = runpy.run_path(str(ROOT / 'ops/cdc/common.py'))
PRIVATE = ROOT / 'secrets/postgresql-ha/sql-pki'
REMOTE = '/etc/crawl-patroni/sql-pki'
CONFIG = '/etc/crawl-patroni/patroni.yml'
HBA = '/etc/postgresql/17/main/pg_hba.conf'
DNS = ['postgres-rw', 'postgres-rw.crawl-validation',
       'postgres-rw.crawl-validation.svc', 'postgres-rw.crawl-validation.svc.cluster.local']


def wait(check, seconds=90):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return
        time.sleep(2)
    raise RuntimeError('SQL TLS condition was not reached before deadline')


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, capture_output=True)


def pki():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (PRIVATE / 'ca.crt').exists():
        assert not (PRIVATE / 'ca.key').exists(), 'Incomplete CA: inspect before retry'
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                '-nodes', '-keyout', PRIVATE / 'ca.key', '-out', PRIVATE / 'ca.crt',
                '-days', '1825', '-sha256', '-subj', '/CN=crawl-postgresql-sql-ca',
                '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    for name, ip in pg['NODES'].items():
        if (PRIVATE / (name + '.crt')).exists():
            extensions = subprocess.check_output(['openssl', 'x509', '-in', str(PRIVATE / (name + '.crt')),
                                                 '-noout', '-ext', 'subjectAltName'], text=True)
            if 'DNS:localhost' in extensions and 'IP Address:127.0.0.1' in extensions:
                continue
        if not (PRIVATE / (name + '.key')).exists():
            openssl('genpkey', '-algorithm', 'EC', '-pkeyopt', 'ec_paramgen_curve:P-256',
                    '-out', PRIVATE / (name + '.key'))
        openssl('req', '-new', '-key', PRIVATE / (name + '.key'), '-out', PRIVATE / (name + '.csr'),
                '-subj', '/CN=crawl-postgresql-' + name)
        ext = ('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
               'extendedKeyUsage=serverAuth\nsubjectAltName=IP:' + ip + ',DNS:' + name +
               ',DNS:localhost,IP:127.0.0.1,IP:::1,' +
               ','.join('DNS:' + n for n in DNS) + '\n')
        (PRIVATE / (name + '.ext')).write_text(ext)
        openssl('x509', '-req', '-in', PRIVATE / (name + '.csr'), '-CA', PRIVATE / 'ca.crt',
                '-CAkey', PRIVATE / 'ca.key', '-CAcreateserial', '-days', '365', '-sha256',
                '-out', PRIVATE / (name + '.crt'), '-extfile', PRIVATE / (name + '.ext'))


def snapshot(node):
    for src, suffix in [(CONFIG, 'patroni.json'), (HBA, 'hba')]:
        dest = PRIVATE / (node + '-before.' + suffix)
        if not dest.exists():
            dest.write_text(nodes['run'](node, 'sudo cat ' + src).stdout)


def config(node):
    return json.loads(nodes['run'](node, 'sudo cat ' + CONFIG).stdout)


def reload_config(node, cfg):
    nodes['put'](node, CONFIG, json.dumps(cfg, indent=2) + '\n', 'postgres')
    nodes['run'](node, 'sudo -u postgres patroni --validate-config --ignore-listen-port ' +
                 CONFIG + ' && sudo systemctl reload crawl-patroni')


def handshake(ip, hostname=None, ca=None):
    context = ssl.create_default_context(cafile=str(ca or PRIVATE / 'ca.crt'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((ip, 5432), timeout=5) as sock:
        sock.sendall(struct.pack('!II', 8, 80877103))
        assert sock.recv(1) == b'S', 'PostgreSQL refused SSLRequest'
        with context.wrap_socket(sock, server_hostname=hostname or ip) as channel:
            return {'version': channel.version(), 'cipher': channel.cipher()[0],
                    'notAfter': channel.getpeercert()['notAfter']}


def prepare_servers():
    pki()
    pg['wait_members']()
    for node, ip in pg['NODES'].items():
        snapshot(node)
        nodes['run'](node, 'sudo install -d -m 0700 -o postgres -g postgres ' + REMOTE)
        for src, dst in [('ca.crt', 'ca.crt'), (node + '.crt', 'server.crt'), (node + '.key', 'server.key')]:
            nodes['put'](node, REMOTE + '/' + dst, (PRIVATE / src).read_text(), 'postgres')
        cfg = config(node)
        cfg['postgresql']['parameters'].update(ssl='on', ssl_cert_file=REMOTE + '/server.crt',
            ssl_key_file=REMOTE + '/server.key', ssl_min_protocol_version='TLSv1.2')
        reload_config(node, cfg)
        wait(lambda: pg['sql'](node, 'SHOW ssl_cert_file;') == REMOTE + '/server.crt')
        pg['sql'](node, 'SELECT pg_reload_conf();')
        # The path can stay unchanged during same-CA renewal; verify the loaded cert.
        wait(lambda: local_name_ready(ip))
        print(json.dumps({'node': node, 'serverTLS': handshake(ip),
                          'stableNameTLS': handshake(ip, DNS[2])}), flush=True)
    # CA contains no private key; Secret keeps provisioning separate from public Git.
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'postgresql-sql-ca',
           'namespace': 'crawl-monitoring'}, 'type': 'Opaque',
           'stringData': {'ca.crt': (PRIVATE / 'ca.crt').read_text()}}
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True,
                   check=True, capture_output=True)


def local_name_ready(ip):
    try:
        handshake(ip, 'localhost')
        return True
    except ssl.SSLCertVerificationError:
        return False


def protected_rules(node, roles):
    """Reject plaintext before all existing grants, including broad subnet grants."""
    old = nodes['run'](node, 'sudo cat ' + HBA).stdout
    marker = '# SQL TLS managed: ' + ','.join(roles)
    end = marker + ' END'
    lines = old.splitlines()
    if marker in lines:
        start, stop = lines.index(marker), lines.index(end)
        del lines[start:stop + 1]
    rules = [marker]
    for role in roles:
        for db in (['all', 'replication'] if role in ['replicator', 'all'] else ['all']):
            for network in ['0.0.0.0/0', '::/0']:
                rules.append('hostnossl ' + db + ' ' + role + ' ' + network + ' reject')
    rules.append(end)
    nodes['put'](node, HBA, '\n'.join(rules + lines) + '\n', 'postgres', 0o640)
    assert pg['sql'](node, 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;') == '0'
    pg['sql'](node, 'SELECT pg_reload_conf();')


def replication():
    pg['wait_members']()
    for ip in pg['NODES'].values():
        handshake(ip)
    original = pg['leader']()
    # Change replicas one at a time, then prepare the primary for its next demotion.
    for node in [n for n in pg['NODES'] if n != original] + [original]:
        snapshot(node)
        cfg = config(node)
        for role in ['replication', 'rewind']:
            cfg['postgresql']['authentication'][role].update(
                sslmode='verify-full', sslrootcert=REMOTE + '/ca.crt')
        reload_config(node, cfg)
        if node != pg['leader']():
            wait(lambda: pg['sql'](node, "SELECT count(*) FROM pg_stat_wal_receiver WHERE status='streaming' AND conninfo LIKE '%sslmode=verify-full%' AND conninfo LIKE '%sslrootcert=" + REMOTE + "/ca.crt%';") == '1')
        pg['wait_members']()
        wait(lambda: bool(pg['api'](node, '/patroni').get('timeline')))
        print(node + ': replication/rewind configured to verify-full', flush=True)
    primary = pg['leader']()
    wait(lambda: pg['sql'](primary, "SELECT count(*) FROM pg_stat_replication r JOIN pg_stat_ssl s USING(pid) WHERE s.ssl AND r.state='streaming' AND r.application_name IN ('s1','s2','s3');") == '2')
    for node in pg['NODES']:
        protected_rules(node, ['replicator', 'patroni_rewind'])
    print('Replication and rewind network roles reject plaintext; local socket access unchanged.', flush=True)


def enforce_grafana():
    pods = json.loads(subprocess.check_output(['kubectl', '-n', 'crawl-monitoring', 'get',
                     'pods', '-l', 'app=grafana', '-o', 'json'], text=True))['items']
    assert len(pods) == 2
    for pod in pods:
        env = {x['name']: x.get('value') for x in pod['spec']['containers'][0]['env']}
        assert env['GF_DATABASE_SSL_MODE'] == 'verify-full'
        assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in pod['status']['conditions'])
    assert pg['sql'](pg['leader'](), "SELECT count(*) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE a.usename='crawl_grafana' AND NOT s.ssl;") == '0'
    assert int(pg['sql'](pg['leader'](), "SELECT count(*) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE a.usename='crawl_grafana' AND s.ssl;")) >= 2
    for node in pg['NODES']:
        protected_rules(node, ['crawl_grafana'])
    print('Grafana database role rejects plaintext on every possible primary.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['servers', 'replication', 'enforce-grafana'])
    parser.add_argument('--execute', required=True, action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    {'servers': prepare_servers, 'replication': replication,
     'enforce-grafana': enforce_grafana}[args.phase]()
