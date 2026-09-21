#!/usr/bin/env python3
"""Dedicated Temporal persistence databases; no application facts in these databases."""
from pathlib import Path
import secrets,subprocess
root=Path(__file__).resolve().parents[2]
d=root/'secrets/temporal';d.mkdir(mode=0o700,exist_ok=True)
for name in ['owner','runtime']:
    f=d/(name+'.env')
    if not f.exists():
        f.write_text('SQL_USER=temporal_validation_'+name+'\nSQL_PASSWORD='+secrets.token_hex(32)+'\n')
        f.chmod(0o600)
values={n:dict(x.split('=',1) for x in (d/(n+'.env')).read_text().splitlines()) for n in ['owner','runtime']}
sql=''
for n,v in values.items():
    role=v['SQL_USER']; password=v['SQL_PASSWORD']
    assert role=='temporal_validation_'+n and len(password)==64 and all(x in '0123456789abcdef' for x in password)
    sql+=f"SELECT 'CREATE ROLE {role} LOGIN' WHERE NOT EXISTS(SELECT FROM pg_roles WHERE rolname='{role}') \\gexec\n"
    limit=64 if n=='runtime' else 32
    sql+=f"ALTER ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {limit} PASSWORD '{password}';\n"
for db in ['temporal_validation','temporal_visibility_validation']:
    sql+=f"SELECT 'CREATE DATABASE {db} OWNER temporal_validation_owner' WHERE NOT EXISTS(SELECT FROM pg_database WHERE datname='{db}') \\gexec\n"
    sql+=f'REVOKE ALL ON DATABASE {db} FROM PUBLIC; GRANT CONNECT ON DATABASE {db} TO temporal_validation_runtime;\n'
ssh=['ssh','-i','/home/ubuntu/.ssh/id_ed25519_crawl_infra','-o','BatchMode=yes']
subprocess.run(ssh+['ubuntu@10.4.4.2','sudo -u postgres psql -X -q -v ON_ERROR_STOP=1'],input=sql,text=True,check=True,stdout=subprocess.DEVNULL)
remote="""sudo python3 - <<'PY'
from pathlib import Path
p=Path('/etc/postgresql/17/main/pg_hba.conf');s=p.read_text();marker='# temporal validation login boundary'
if marker not in s:
    p.with_suffix('.conf.pre-temporal').write_text(s)
    rules=marker+'\\n'
    for role in ['temporal_validation_owner','temporal_validation_runtime']:
        rules+='hostssl temporal_validation,temporal_visibility_validation '+role+' 10.4.4.0/22 scram-sha-256\\n'
        rules+='host all '+role+' 0.0.0.0/0 reject\\nhost all '+role+' ::0/0 reject\\n'
    p.write_text(rules+s)
PY
sudo -u postgres psql -X -Atc 'SELECT pg_reload_conf()' >/dev/null
"""
for ip in ['10.4.4.2','10.4.4.8','10.4.4.5']:
    subprocess.run(ssh+['ubuntu@'+ip,'bash -s'],input=remote,text=True,check=True)
print('Temporal validation databases and restricted logins prepared.')
