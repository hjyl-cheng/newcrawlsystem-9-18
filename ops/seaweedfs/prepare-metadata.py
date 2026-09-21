#!/usr/bin/env python3
"""Prepare the PG metadata store and loopback primary selectors; does not start filers."""
import json
import os
from pathlib import Path
import runpy
import secrets

ROOT = Path(__file__).resolve().parents[2]
h = runpy.run_path(str(ROOT / 'ops/backup/bootstrap-pgbackrest.py'))
run, put = h['run'], h['put']
NODES = {'s1':'10.4.4.2', 's2':'10.4.4.8', 's3':'10.4.4.5'}
PRIVATE = ROOT / 'secrets/seaweedfs'

if __name__ == '__main__':
    os.umask(0o077)
    sql_ca = ROOT / 'secrets/postgresql-ha/sql-pki/ca.crt'
    assert sql_ca.exists(), 'Prepare PostgreSQL SQL TLS before generating Filer metadata settings'
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    secret = PRIVATE / 'db-password'
    if not secret.exists(): secret.write_text(secrets.token_hex(32) + '\n')
    secret.chmod(0o600)
    password = secret.read_text().strip()
    assert len(password) == 64 and all(c in '0123456789abcdef' for c in password)
    primaries = [ip for ip in NODES.values() if run(ip, 'sudo -u postgres psql -XAt -c "select pg_is_in_recovery();"', capture=True).stdout.strip() == 'f']
    assert len(primaries) == 1
    sql = "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='seaweedfs_filer') THEN CREATE ROLE seaweedfs_filer LOGIN; END IF; END $$;\n"
    sql += f"ALTER ROLE seaweedfs_filer PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 12;\n"
    sql += (ROOT / 'ops/seaweedfs/metadata-schema.sql').read_text()
    run(primaries[0], 'sudo -u postgres psql -Xq -v ON_ERROR_STOP=1 -d crawler', sql, capture=True)
    for name, ip in NODES.items():
        hba_path = '/etc/postgresql/17/main/pg_hba.conf'
        old = run(ip, 'sudo cat ' + hba_path, capture=True).stdout
        if '# SeaweedFS metadata role' not in old:
            prefix = '# SeaweedFS metadata role\n' + ''.join(f'hostssl crawler seaweedfs_filer {source}/32 scram-sha-256\n' for source in ['127.0.0.1',*NODES.values()])
            prefix += 'host all seaweedfs_filer 0.0.0.0/0 reject\nhost all seaweedfs_filer ::0/0 reject\n'
            put(ip, hba_path, prefix + old, 'postgres', 0o640)
        run(ip, 'sudo -u postgres psql -XAt -c "select pg_reload_conf();"', capture=True)
        users = run(ip, 'sudo cat /etc/pgbouncer/userlist.txt', capture=True).stdout
        users = '\n'.join(line for line in users.splitlines() if not line.startswith('"seaweedfs_filer"')) + f'\n"seaweedfs_filer" "{password}"\n'
        put(ip, '/etc/pgbouncer/userlist.txt', users, 'postgres', 0o600)
        run(ip, 'sudo systemctl reload pgbouncer', capture=True)
        run(ip, 'sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq haproxy', capture=True)
        # The generic packaged listener is unused; the dedicated service binds loopback only.
        run(ip, 'sudo systemctl disable --now haproxy', capture=True)
        run(ip, 'sudo install -d -m 0750 -o root -g haproxy /etc/seaweedfs/patroni-check', capture=True)
        pki = ROOT / 'secrets/postgresql-ha/rest-pki'
        put(ip, '/etc/seaweedfs/patroni-check/ca.crt', (pki/'ca.crt').read_text(), mode=0o644)
        put(ip, '/etc/seaweedfs/patroni-check/client.pem', (pki/'client.crt').read_text() + (pki/'client.key').read_text(), mode=0o600)
        put(ip, '/etc/seaweedfs/metadata-haproxy.cfg', (ROOT/'ops/seaweedfs/metadata-haproxy.cfg').read_text(), mode=0o644)
        put(ip, '/etc/seaweedfs/metadata-initial.state', (ROOT/'ops/seaweedfs/metadata-initial.state').read_text(), mode=0o644)
        service = '''[Unit]
Description=SeaweedFS local PostgreSQL primary selector
After=network-online.target
Wants=network-online.target
[Service]
RuntimeDirectory=crawl-seaweed-pg
RuntimeDirectoryMode=0750
ExecStart=/usr/sbin/haproxy -W -db -f /etc/seaweedfs/metadata-haproxy.cfg
Restart=on-failure
RestartSec=2
LimitNOFILE=4096
[Install]
WantedBy=multi-user.target
'''
        put(ip, '/etc/systemd/system/crawl-seaweed-pg.service', service, mode=0o644)
        run(ip, 'sudo /usr/sbin/haproxy -c -f /etc/seaweedfs/metadata-haproxy.cfg', capture=True)
        run(ip, 'sudo systemctl daemon-reload && sudo systemctl enable --now crawl-seaweed-pg', capture=True)
        # Pending config: activated only during explicit metadata migration.
        cfg = f'''[postgres]
enabled = true
createTable = false
hostname = "127.0.0.1"
port = 15432
username = "seaweedfs_filer"
password = "{password}"
database = "crawler"
sslmode = "verify-full"
sslrootcert = "/etc/seaweedfs/postgresql-ca.crt"
pgbouncer_compatible = true
connection_max_idle = 4
connection_max_open = 0
connection_max_lifetime_seconds = 60
enableUpsert = true
'''
        put(ip, '/etc/seaweedfs/postgresql-ca.crt', sql_ca.read_text(), 'seaweedfs', 0o600)
        put(ip, '/etc/seaweedfs/filer-postgres.pending.toml', cfg, 'seaweedfs', 0o600)
        print(name + ': metadata account, primary selector and pending config prepared')
