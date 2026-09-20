#!/usr/bin/env python3
"""Prepare existing PG directories for Patroni; deliberately does not start it."""
import json
import runpy
import secrets
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
b = runpy.run_path(str(Path(__file__).with_name('bootstrap-coordination.py')))
run, put, NODES, PRIVATE = (b[k] for k in ('run', 'put', 'NODES', 'PRIVATE'))
REST = PRIVATE / 'rest-pki'

def envfile(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        k, v = line.split('=', 1)
        values[k] = shlex.split(v)[0] if v.startswith(('"', "'")) else v
    return values

def password(name):
    p = PRIVATE / name
    if not p.exists(): p.write_text(secrets.token_hex(32) + '\n')
    p.chmod(0o600)
    return p.read_text().strip()

def rest_pki():
    REST.mkdir(mode=0o700, exist_ok=True)
    openssl = b['openssl']
    if not (REST / 'ca.crt').exists():
        assert not (REST / 'ca.key').exists()
        openssl('req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes',
                '-keyout', REST / 'ca.key', '-out', REST / 'ca.crt', '-days', '1825', '-sha256',
                '-subj', '/CN=crawl-patroni-rest-ca', '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        (REST / 'ca.key').chmod(0o600)
    # The reusable helper only issues certificates. This CA is distinct from the etcd CA.
    certificate = b['certificate']
    certificate.__globals__['PKI'] = REST
    for name, ip in NODES.items(): certificate(name, 'patroni-' + name, f'IP:{ip},DNS:{name}')
    certificate('client', 'patroni-rest-client')

if __name__ == '__main__':
    rest_pki()
    replication = envfile(ROOT / 'secrets/validation-storage.env')['REPLICATION_PASSWORD']
    rewind, api = password('rewind-password'), password('rest-password')
    # Credential goes over SSH stdin, not argv or terminal output.
    sql = """SET log_statement='none';
DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='patroni_rewind') THEN CREATE ROLE patroni_rewind LOGIN; END IF; END $$;
ALTER ROLE patroni_rewind WITH PASSWORD '%s';
GRANT EXECUTE ON FUNCTION pg_catalog.pg_ls_dir(text,boolean,boolean) TO patroni_rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_stat_file(text,boolean) TO patroni_rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text) TO patroni_rewind;
GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_binary_file(text,bigint,bigint,boolean) TO patroni_rewind;
""" % rewind
    run(NODES['s1'], 'sudo -n -u postgres psql -Xq -v ON_ERROR_STOP=1', sql, capture=True)
    # Preserve actual admission rules, with loopback access for the restricted ingestor before its reject rule.
    hba = run(NODES['s1'], 'sudo -n cat /etc/postgresql/17/main/pg_hba.conf', capture=True).stdout
    hba = 'host crawler_validation_ingestor crawler_ingestor_validation 127.0.0.1/32 scram-sha-256\n' + hba
    hba = '\n'.join(f'host postgres patroni_rewind {ip}/32 scram-sha-256' for ip in NODES.values()) + '\nhost all patroni_rewind 0.0.0.0/0 reject\nhost all patroni_rewind ::0/0 reject\n' + hba
    for name, ip in NODES.items():
        run(ip, 'sudo -n install -d -m 0700 -o postgres -g postgres /etc/crawl-patroni /etc/crawl-patroni/pki /etc/crawl-patroni/rest-pki; sudo -n install -d -m 0700 /srv/crawlsystem/pg-ha/pre-patroni; sudo -n cp -an /etc/postgresql/17/main /srv/crawlsystem/pg-ha/pre-patroni/')
        for src, dst in [('ca.crt', 'ca.crt'), ('gateway.crt', 'client.crt'), ('gateway.key', 'client.key')]:
            put(ip, '/etc/crawl-patroni/pki/' + dst, (PRIVATE / 'pki' / src).read_text(), 'postgres')
        for src, dst in [('ca.crt', 'ca.crt'), (name + '.crt', 'server.crt'), (name + '.key', 'server.key'), ('client.crt', 'client.crt'), ('client.key', 'client.key')]:
            put(ip, '/etc/crawl-patroni/rest-pki/' + dst, (REST / src).read_text(), 'postgres')
        cfg = {
            'scope': 'crawl-pg', 'namespace': '/service/', 'name': name,
            'restapi': {'listen': ip + ':8008', 'connect_address': ip + ':8008',
                'certfile': '/etc/crawl-patroni/rest-pki/server.crt', 'keyfile': '/etc/crawl-patroni/rest-pki/server.key',
                'cafile': '/etc/crawl-patroni/rest-pki/ca.crt', 'verify_client': 'required',
                'authentication': {'username': 'operator', 'password': api}},
            'ctl': {'cacert': '/etc/crawl-patroni/rest-pki/ca.crt', 'certfile': '/etc/crawl-patroni/rest-pki/client.crt',
                    'keyfile': '/etc/crawl-patroni/rest-pki/client.key'},
            'etcd3': {'hosts': ','.join(ip + ':2379' for ip in NODES.values()), 'protocol': 'https',
                'cacert': '/etc/crawl-patroni/pki/ca.crt', 'cert': '/etc/crawl-patroni/pki/client.crt',
                'key': '/etc/crawl-patroni/pki/client.key', 'username': 'patroni',
                'password': (PRIVATE / 'dcs-password').read_text().strip()},
            'bootstrap': {'dcs': {'ttl': 30, 'loop_wait': 5, 'retry_timeout': 5,
                'maximum_lag_on_failover': 1048576, 'check_timeline': True,
                'synchronous_mode': True, 'synchronous_mode_strict': True, 'synchronous_node_count': 1,
                'postgresql': {'use_pg_rewind': True, 'use_slots': True, 'parameters': {
                    'wal_level': 'replica', 'wal_log_hints': 'on', 'hot_standby': 'on',
                    'max_connections': 200, 'max_wal_senders': 10, 'max_replication_slots': 10,
                    'wal_keep_size': '512MB', 'max_slot_wal_keep_size': '2GB',
                    'synchronous_commit': 'on', 'archive_mode': 'on', 'archive_timeout': '300s',
                    'archive_command': '/usr/bin/pgbackrest --config=/etc/pgbackrest/pg-node.conf --stanza=crawler archive-push %p'}}}},
            'postgresql': {'listen': '0.0.0.0:5432', 'connect_address': ip + ':5432',
                'data_dir': '/var/lib/postgresql/17/main', 'config_dir': '/etc/postgresql/17/main',
                'bin_dir': '/usr/lib/postgresql/17/bin', 'use_unix_socket': True,
                'pgpass': '/var/lib/postgresql/.pgpass-patroni',
                'authentication': {'superuser': {'username': 'postgres'},
                    'replication': {'username': 'replicator', 'password': replication},
                    'rewind': {'username': 'patroni_rewind', 'password': rewind}},
                'parameters': {'unix_socket_directories': '/var/run/postgresql', 'shared_buffers': '512MB',
                    'password_encryption': 'scram-sha-256', 'hba_file': '/etc/postgresql/17/main/pg_hba.conf',
                    'ident_file': '/etc/postgresql/17/main/pg_ident.conf'},
                'use_pg_rewind': True, 'remove_data_directory_on_rewind_failure': False,
                'remove_data_directory_on_diverged_timelines': False,
                'create_replica_methods': ['manual_only'], 'manual_only': {'command': '/bin/false'}},
            'watchdog': {'mode': 'required', 'device': '/dev/watchdog', 'safety_margin': 10},
            'tags': {'nofailover': False, 'noloadbalance': False, 'nosync': False},
        }
        put(ip, '/etc/crawl-patroni/patroni.yml', json.dumps(cfg, indent=2), 'postgres')
        put(ip, '/etc/postgresql/17/main/pg_hba.conf', hba, 'postgres', 0o640)
        put(ip, '/etc/modules-load.d/crawl-watchdog.conf', 'softdog\n', mode=0o644)
        put(ip, '/etc/modprobe.d/crawl-watchdog.conf', 'options softdog nowayout=0\n', mode=0o644)
        put(ip, '/etc/udev/rules.d/60-crawl-watchdog.rules', 'KERNEL=="watchdog", OWNER="postgres", GROUP="postgres", MODE="0600"\nKERNEL=="watchdog0", OWNER="postgres", GROUP="postgres", MODE="0600"\n', mode=0o644)
        put(ip, '/etc/systemd/system/crawl-patroni.service', (ROOT / 'ops/postgresql-ha/crawl-patroni.service').read_text(), mode=0o644)
        run(ip, 'sudo -n chown postgres:postgres /dev/watchdog /dev/watchdog0 && sudo -n udevadm control --reload-rules && sudo -n systemctl daemon-reload && sudo -n -u postgres patroni --validate-config --ignore-listen-port /etc/crawl-patroni/patroni.yml', capture=True)
        run(ip, 'sudo -n -u postgres psql -XAt -c "select pg_reload_conf();"', capture=True)
        print(name + ': protected config validated; Patroni NOT started.')
