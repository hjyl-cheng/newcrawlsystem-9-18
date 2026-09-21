#!/usr/bin/env python3
"""Explicit bootstrap for local disks, existing PG database and private Secrets."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ops/cdc'))
import common as pg
spec = importlib.util.spec_from_file_location('nodes', ROOT / 'ops/monitoring/bootstrap-nodes.py')
nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nodes)


def apply(obj):
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True,
                   check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.parse_args()
    os.umask(0o077)
    private = ROOT / 'secrets/monitoring'
    for name in ['grafana-db-password', 'grafana-secret-key']:
        if not (private / name).exists():
            (private / name).write_text(secrets.token_hex(32))
    admin = private / 'grafana-admin-password'
    if not admin.exists():
        raise RuntimeError('Provide protected secrets/monitoring/grafana-admin-password first')
    password = (private / 'grafana-db-password').read_text().strip()
    primary = pg.leader()
    pg.sql(primary, "SET log_statement='none'; DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='crawl_grafana') THEN CREATE ROLE crawl_grafana LOGIN; END IF; END $$; ALTER ROLE crawl_grafana PASSWORD '" + password + "' NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 50;")
    if pg.sql(primary, "SELECT count(*) FROM pg_database WHERE datname='crawler_grafana';") == '0':
        pg.sql(primary, 'CREATE DATABASE crawler_grafana OWNER crawl_grafana;')
    pg.sql(primary, 'REVOKE ALL ON DATABASE crawler_grafana FROM PUBLIC;')
    for node in pg.NODES:
        path = '/etc/postgresql/17/main/pg_hba.conf'
        old = nodes.run(node, 'sudo cat ' + path).stdout
        clean = '\n'.join(l for l in old.splitlines() if 'crawl_grafana' not in l)
        rules = '\n'.join('host crawler_grafana crawl_grafana ' + ip + '/32 scram-sha-256' for n, ip in nodes.NODES.items() if n.startswith('a'))
        rules += '\nhost all crawl_grafana 0.0.0.0/0 reject\nhost all crawl_grafana ::/0 reject\n'
        nodes.put(node, path, rules + clean + '\n', 'postgres', 0o640)
        pg.sql(node, 'SELECT pg_reload_conf();')
    apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'crawl-monitoring'}})
    def secret(name, data):
        apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': 'crawl-monitoring'}, 'type': 'Opaque', 'stringData': data})
    secret('monitoring-client-tls', {n: (private / n).read_text() for n in ['ca.crt', 'prometheus-client.crt', 'prometheus-client.key']})
    secret('grafana-private', {'GF_DATABASE_PASSWORD': password, 'GF_SECURITY_SECRET_KEY': (private / 'grafana-secret-key').read_text().strip(), 'GF_SECURITY_ADMIN_PASSWORD': admin.read_text().strip()})
    for node in ['a1', 'a2', 'a3']:
        paths = ['/srv/crawlsystem/monitoring/alertmanager']
        if node != 'a1': paths.append('/srv/crawlsystem/monitoring/prometheus')
        nodes.run(node, 'sudo install -d -m 0750 -o 65534 -g 65534 ' + ' '.join(paths))
    subprocess.run(['kubectl', 'apply', '-f', str(ROOT / 'ops/monitoring/cluster-resources.yaml')], check=True)
    print('Monitoring database, private Secrets and local volumes prepared; no credentials printed.')


if __name__ == '__main__':
    main()
