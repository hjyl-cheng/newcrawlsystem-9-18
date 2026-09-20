#!/usr/bin/env python3
"""Restore archived WAL to a named point in a fresh S3 directory; never touch live PGDATA."""
from pathlib import Path
import json
import runpy
import shlex
import time
import uuid

m = runpy.run_path(str(Path(__file__).with_name('bootstrap-pgbackrest.py')))
run, put = m['run'], m['put']
S1, S2, S3 = m['S1'], m['S2'], m['S3']
tag = uuid.uuid4().hex[:16]
schema = 'backup_verify_' + tag
target = 'crawl_restore_' + tag
directory = '/srv/crawlsystem/restore-check-' + tag
data = directory + '/data'
socket = directory + '/socket'
port = 55432

def sql(ip, statement, restored=False, database='postgres'):
    command = ['sudo', '-n', '-u', 'postgres', 'psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-d', database]
    if restored: command += ['-h', socket, '-p', str(port)]
    return run(ip, shlex.join(command), statement, capture=True).stdout.strip()

primaries = [ip for ip in (S1, S2, S3) if sql(ip, 'SELECT pg_is_in_recovery();') == 'f']
assert len(primaries) == 1, 'Require exactly one writable primary'
PRIMARY = primaries[0]

started = False
created = False
try:
    # A full backup must predate this marker, so seeing it requires archived WAL replay.
    info = json.loads(run(S2, 'sudo -n -u pgbackrest pgbackrest --stanza=crawler --output=json info', capture=True).stdout)
    backups = info[0].get('backup', [])
    assert backups, 'Run and validate the first full backup before restoring'
    selected = backups[-1]['label']
    sql(PRIMARY, f'CREATE SCHEMA {schema}; CREATE TABLE {schema}.marker(value text PRIMARY KEY); INSERT INTO {schema}.marker VALUES (\'before\');')
    created = True
    before = sql(PRIMARY, "SELECT receipt_id,submission_id,content_sha256 FROM ingestion.receipts ORDER BY receipt_id;", database='crawler_validation_ingestor')
    sql(PRIMARY, f"SELECT pg_create_restore_point('{target}');")
    sql(PRIMARY, f"INSERT INTO {schema}.marker VALUES ('after'); SELECT pg_switch_wal();")
    run(S2, 'sudo -n -u pgbackrest pgbackrest --stanza=crawler check')
    assert sql(PRIMARY, f'SELECT count(*) FROM {schema}.marker;') == '2'
    run(S3, 'sudo -n test ! -e ' + shlex.quote(directory))
    run(S3, f'sudo -n install -d -m 0700 -o postgres -g postgres {directory} {data} {socket}')
    run(S3, shlex.join(['sudo', '-n', '-u', 'postgres', 'pgbackrest', '--stanza=crawler',
        '--pg1-path=' + data, '--set=' + selected, '--type=name', '--target=' + target,
        '--target-action=promote', '--archive-mode=off', 'restore']))
    # Debian keeps live config outside PGDATA; provide isolated config, no TCP and no replication.
    put(S3, directory + '/postgresql.conf', f"""data_directory = '{data}'
hba_file = '{directory}/pg_hba.conf'
ident_file = '{directory}/pg_ident.conf'
listen_addresses = ''
port = {port}
unix_socket_directories = '{socket}'
unix_socket_permissions = 0700
shared_buffers = '64MB'
max_connections = 200
hot_standby = on
archive_mode = off
archive_command = ''
primary_conninfo = ''
primary_slot_name = ''
synchronous_standby_names = ''
""", 'postgres')
    put(S3, directory + '/pg_hba.conf', 'local all all peer\n', 'postgres')
    put(S3, directory + '/pg_ident.conf', '', 'postgres')
    run(S3, shlex.join(['sudo', '-n', '-u', 'postgres', '/usr/lib/postgresql/17/bin/pg_ctl',
        '-D', data, '-l', directory + '/postgres.log', '-o', '-c config_file=' + directory + '/postgresql.conf',
        '-w', '-t', '120', 'start']))
    started = True
    for _ in range(60):
        if sql(S3, 'SELECT pg_is_in_recovery();', restored=True) == 'f': break
        time.sleep(1)
    else: raise RuntimeError('Restored cluster did not reach the named target')
    assert sql(S3, f'SELECT value FROM {schema}.marker ORDER BY value;', restored=True) == 'before'
    after = sql(S3, 'SELECT receipt_id,submission_id,content_sha256 FROM ingestion.receipts ORDER BY receipt_id;',
                restored=True, database='crawler_validation_ingestor')
    assert before == after, 'Restored ingestion receipts differ'
    databases = sql(S3, 'SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname;', restored=True).splitlines()
    assert {'crawler', 'crawler_validation_ingestor', 'temporal_validation', 'temporal_visibility_validation'}.issubset(databases)
    print(json.dumps(dict(status='PASSED', backup=selected, restorePoint=target, directory=directory,
        beforeRecovered=True, afterExcluded=True, receiptsMatch=True, databases=databases)))
finally:
    # Stop only the isolated directory, retaining evidence; never stop main PG on S3.
    if started:
        run(S3, shlex.join(['sudo', '-n', '-u', 'postgres', '/usr/lib/postgresql/17/bin/pg_ctl', '-D', data, '-m', 'fast', '-w', 'stop']))
    if created: sql(PRIMARY, f'DROP SCHEMA {schema} CASCADE;')
