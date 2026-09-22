#!/usr/bin/env python3
"""Read-only PostgreSQL role for the standard exporter. Does not restart PostgreSQL."""
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

ROLE = 'crawl_pg_exporter'
MARKER = '# standard pg exporter login'
END = MARKER + ' END'
HBA = '/etc/postgresql/17/main/pg_hba.conf'


def apply_secret(password):
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'pg-exporter-private',
           'namespace': 'crawl-monitoring'}, 'type': 'Opaque',
           'stringData': {'password': password}}
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(obj), text=True,
                   check=True, capture_output=True)


def hba_block():
    sources = [ip for name, ip in nodes.NODES.items() if name.startswith('a')]
    lines = [MARKER]
    for ip in sources:
        lines.append('hostssl postgres ' + ROLE + ' ' + ip + '/32 scram-sha-256')
    lines.append('host all ' + ROLE + ' 0.0.0.0/0 reject')
    lines.append('host all ' + ROLE + ' ::/0 reject')
    lines.append(END)
    return lines


def install_hba(node):
    old = nodes.run(node, 'sudo cat ' + HBA).stdout.splitlines()
    if MARKER in old:
        start, stop = old.index(MARKER), old.index(END)
        del old[start:stop + 1]
    # After the global plaintext rejects, before role-specific grants.
    insert = next(i for i, line in enumerate(old) if line.startswith('local '))
    nodes.put(node, HBA, '\n'.join(old[:insert] + hba_block() + old[insert:]) + '\n', 'postgres', 0o640)
    assert pg.sql(node, 'SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;') == '0'
    pg.sql(node, 'SELECT pg_reload_conf();')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.parse_args()
    os.umask(0o077)
    private = ROOT / 'secrets/monitoring' / 'pg-exporter-password'
    if not private.exists():
        private.write_text(secrets.token_hex(32) + '\n')
    password = private.read_text().strip()
    assert password and 'crawl_grafana' not in ROLE
    primary = pg.leader()
    statement = (
        "SET log_statement='none'; "
        "DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='" + ROLE + "') "
        "THEN CREATE ROLE " + ROLE + " LOGIN; END IF; END $$; "
        "ALTER ROLE " + ROLE + " PASSWORD '" + password.replace("'", "''") + "' "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 4; "
        "GRANT pg_monitor TO " + ROLE + "; "
        "GRANT CONNECT ON DATABASE postgres TO " + ROLE + ";"
    )
    pg.sql(primary, statement)
    check = pg.sql(primary, "SELECT rolsuper::text || ' ' || rolcreatedb::text || ' ' || rolcreaterole::text || ' ' || rolconnlimit::text || ' ' || (pg_has_role('" + ROLE + "','pg_monitor','member'))::text FROM pg_roles WHERE rolname='" + ROLE + "';")
    assert check == 'false false false 4 true', check
    for node in pg.NODES:
        install_hba(node)
    apply_secret(password)
    visible = pg.sql(primary, "SELECT count(*) FROM pg_hba_file_rules WHERE '" + ROLE + "' = ANY(user_name) AND error IS NULL;")
    print('Read-only ' + ROLE + ' ready on ' + primary + '; hba rules ' + visible + '; password not printed.')


if __name__ == '__main__':
    main()
