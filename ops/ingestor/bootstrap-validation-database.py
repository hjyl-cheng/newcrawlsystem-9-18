#!/usr/bin/env python3
"""Prepare only the dedicated validation database/role. Never migrates crawler."""
from pathlib import Path
import re
import subprocess
root = Path(__file__).resolve().parents[2]
env = dict(line.split('=', 1) for line in (root/'secrets/data-ingestor/runtime.env').read_text().splitlines())
password = env['PGPASSWORD']
assert re.fullmatch('[0-9a-f]{64}', password)
assert env['PGDATABASE'] == 'crawler_validation_ingestor'
ssh = ['ssh', '-i', '/home/ubuntu/.ssh/id_ed25519_crawl_infra', '-o', 'BatchMode=yes', 'ubuntu@10.4.4.2']
sql = r"""
SELECT 'CREATE ROLE crawler_ingestor_validation LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 16'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='crawler_ingestor_validation') \gexec
ALTER ROLE crawler_ingestor_validation PASSWORD '%s';
ALTER ROLE crawler_ingestor_validation SET statement_timeout='30s';
ALTER ROLE crawler_ingestor_validation SET idle_in_transaction_session_timeout='45s';
SELECT 'CREATE DATABASE crawler_validation_ingestor OWNER crawler'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='crawler_validation_ingestor') \gexec
REVOKE ALL ON DATABASE crawler_validation_ingestor FROM PUBLIC;
GRANT CONNECT ON DATABASE crawler_validation_ingestor TO crawler_ingestor_validation;
""" % password
subprocess.run(ssh+['sudo -u postgres psql -X -v ON_ERROR_STOP=1 -q'], input=sql, text=True, check=True, stdout=subprocess.DEVNULL)
# Restrict this login to its validation DB and private subnet, before general HBA rules.
remote = """sudo python3 - <<'PY'
from pathlib import Path
p=Path('/etc/postgresql/17/main/pg_hba.conf')
s=p.read_text()
marker='# data-ingestor validation login boundary'
if marker not in s:
    p.with_suffix('.conf.pre-ingestor').write_text(s)
    p.write_text(marker+'\\nhostssl crawler_validation_ingestor crawler_ingestor_validation 10.4.4.0/22 scram-sha-256\\nhost all crawler_ingestor_validation 0.0.0.0/0 reject\\nhost all crawler_ingestor_validation ::0/0 reject\\n'+s)
PY
sudo -u postgres psql -X -Atc 'SELECT pg_reload_conf()' >/dev/null
"""
for address in ['10.4.4.2', '10.4.4.8', '10.4.4.5']:
    peer = ssh[:-1] + ['ubuntu@'+address]
    subprocess.run(peer+['bash -s'],input=remote,text=True,check=True)
print('Dedicated validation database/login prepared; password omitted.')
