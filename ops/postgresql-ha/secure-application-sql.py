#!/usr/bin/env python3
"""Migrate existing application SQL transports in compatibility-first phases."""
import argparse
import configparser
import io
import json
import os
import runpy
import secrets
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
t = runpy.run_path(str(Path(__file__).with_name('secure-sql-transport.py')))
pg, nodes = t['pg'], t['nodes']
PRIVATE = ROOT / 'secrets/postgresql-ha/sql-client-migration'
POOL = '/etc/pgbouncer/pgbouncer.ini'
FILER = '/etc/seaweedfs/filer.toml'
ADMIN = ROOT / 'secrets/postgresql-ha/pgbouncer-admin-password'


def snapshot(node, path):
    dest = PRIVATE / (node + '-' + path.rsplit('/', 1)[-1])
    if not dest.exists():
        dest.write_text(nodes['run'](node, 'sudo cat ' + path).stdout)


def pool_config(node):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(nodes['run'](node, 'sudo cat ' + POOL).stdout)
    return cfg


def put_pool(node, cfg):
    stream = io.StringIO()
    cfg.write(stream)
    nodes['put'](node, POOL, stream.getvalue(), 'postgres', 0o640)
    nodes['run'](node, 'sudo systemctl reload pgbouncer')


def pool_admin(node, statement):
    code = '''import json,sys,psycopg2
x=json.load(sys.stdin)
c=psycopg2.connect(host='/var/run/postgresql',port=6432,user='crawl_pool_operator',password=x['password'],dbname='pgbouncer',connect_timeout=5)
c.autocommit=True
with c.cursor() as q:
 q.execute(x['sql'])
 print(json.dumps([dict(zip([d[0] for d in q.description],r)) for r in q.fetchall()] if q.description else [],default=str))
c.close()
'''
    return json.loads(nodes['run'](node, 'sudo -u postgres python3 -c ' + shlex.quote(code),
        json.dumps({'password': ADMIN.read_text().strip(), 'sql': statement})).stdout)


def prepare():
    pg['wait_members']()
    if not ADMIN.exists():
        ADMIN.write_text(secrets.token_hex(32) + '\n')
    ca = (t['PRIVATE'] / 'ca.crt').read_text()
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'postgresql-sql-ca',
           'namespace': 'crawl-validation'}, 'type': 'Opaque', 'stringData': {'ca.crt': ca}}
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True, check=True, capture_output=True)
    for node in pg['NODES']:
        for path in [POOL, FILER, '/etc/pgbouncer/userlist.txt', t['HBA']]:
            snapshot(node, path)
        cfg = pool_config(node)
        # Both processes already run as postgres. Reuse this host's verified SQL identity.
        section = cfg['pgbouncer']
        section.update(client_tls_sslmode=section.get('client_tls_sslmode', 'allow'),
            client_tls_cert_file=t['REMOTE'] + '/server.crt', client_tls_key_file=t['REMOTE'] + '/server.key',
            client_tls_protocols='secure', server_tls_sslmode='verify-full',
            server_tls_ca_file=t['REMOTE'] + '/ca.crt', server_tls_protocols='secure')
        section['admin_users'] = ','.join(dict.fromkeys(section.get('admin_users', '').replace(',', ' ').split() + ['crawl_pool_operator']))
        old = nodes['run'](node, 'sudo cat /etc/pgbouncer/userlist.txt').stdout
        users = [l for l in old.splitlines() if not l.startswith('"crawl_pool_operator"')]
        users.append('"crawl_pool_operator" "' + ADMIN.read_text().strip() + '"')
        nodes['put'](node, '/etc/pgbouncer/userlist.txt', '\n'.join(users) + '\n', 'postgres')
        put_pool(node, cfg)
        pool_admin(node, 'RECONNECT;')
        actual = {r['key']: r['value'] for r in pool_admin(node, 'SHOW CONFIG;')}
        assert actual['server_tls_sslmode'] == 'verify-full'
        assert actual['client_tls_sslmode'] in ['allow', 'require']
        print(node + ': pool accepts TLS; backend verifies local PG certificate', flush=True)


def filer_ready(node):
    request = urllib.request.Request('http://' + pg['NODES'][node] + ':8888/?pretty=y',
                                     headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as res:
        return bool(json.load(res).get('Entries'))


def seaweed():
    for node in pg['NODES']:
        snapshot(node, FILER)
        ca = (t['PRIVATE'] / 'ca.crt').read_text()
        old_ca = nodes['run'](node, 'sudo cat /etc/seaweedfs/postgresql-ca.crt 2>/dev/null || true').stdout
        nodes['put'](node, '/etc/seaweedfs/postgresql-ca.crt', ca, 'seaweedfs', 0o600)
        old = nodes['run'](node, 'sudo cat ' + FILER).stdout
        # Only change these two keys in the existing [postgres] section; preserve passwords and store settings.
        lines = old.splitlines(); output = []; in_pg = False; changed = False
        for line in lines:
            if line.strip().startswith('['):
                in_pg = line.strip() == '[postgres]'
            if in_pg and line.strip().startswith('sslrootcert'):
                continue
            if in_pg and line.strip().startswith('sslmode'):
                output += ['sslmode = "verify-full"', 'sslrootcert = "/etc/seaweedfs/postgresql-ca.crt"']
                changed = True
            else:
                output.append(line)
        assert changed
        value = '\n'.join(output) + '\n'
        nodes['put'](node, FILER, value, 'seaweedfs')
        nodes['put'](node, '/etc/seaweedfs/filer-postgres.pending.toml', value, 'seaweedfs')
        # systemd bounds the old process shutdown; keep the other two filers serving.
        live = [r for r in pool_admin(pg['leader'](), 'SHOW CLIENTS;')
                if r['user'] == 'seaweedfs_filer' and r.get('addr') == pg['NODES'][node]]
        if old != value or old_ca != ca or not live or not all(r.get('tls') for r in live):
            nodes['run'](node, 'sudo systemctl restart --no-block seaweedfs-filer')
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            state = nodes['run'](node, 'systemctl show seaweedfs-filer -p ActiveState --value').stdout.strip()
            try:
                if state == 'active' and filer_ready(node):
                    clients = pool_admin(pg['leader'](), 'SHOW CLIENTS;')
                    if any(r['user'] == 'seaweedfs_filer' and r.get('tls') and r.get('addr') == pg['NODES'][node] for r in clients):
                        break
            except (OSError, ValueError):
                pass
            time.sleep(2)
        else:
            raise RuntimeError(node + ': new Filer did not establish TLS pool connection')
        print(node + ': Filer restarted; real metadata connection uses TLS', flush=True)


def enforce():
    pg['wait_members']()
    for node in pg['NODES']:
        assert pg['sql'](node, 'SELECT count(*) FROM pg_stat_activity a JOIN pg_stat_ssl s USING(pid) WHERE client_addr IS NOT NULL AND NOT ssl;') == '0', node + ': PG still has plaintext clients'
        clients = pool_admin(node, 'SHOW CLIENTS;')
        assert all(r.get('tls') for r in clients if r.get('addr') not in ['unix', None]), node + ': pool still has plaintext clients'
    for node in pg['NODES']:
        cfg = pool_config(node)
        cfg['pgbouncer']['client_tls_sslmode'] = 'require'
        put_pool(node, cfg)
        t['protected_rules'](node, ['all'])
        assert {r['key']: r['value'] for r in pool_admin(node, 'SHOW CONFIG;')}['client_tls_sslmode'] == 'require'
        print(node + ': all PG and PgBouncer TCP clients must use TLS', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'seaweed', 'enforce'])
    parser.add_argument('--execute', required=True, action='store_true')
    args = parser.parse_args(); os.umask(0o077)
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    {'prepare': prepare, 'seaweed': seaweed, 'enforce': enforce}[args.phase]()
