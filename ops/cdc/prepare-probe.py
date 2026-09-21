#!/usr/bin/env python3
"""Create a least-privilege CDC probe, exact SQL publication, protected K8s secret."""
import argparse,json,os,secrets,subprocess
import common as c

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',required=True,action='store_true');p.parse_args()
 os.umask(0o077);folder=c.ROOT/'secrets/cdc';folder.mkdir(mode=0o700,exist_ok=True)
 path=folder/'db-password'
 if not path.exists():path.write_text(secrets.token_hex(32)+'\n')
 path.chmod(0o600);password=path.read_text().strip();assert len(password)==64 and all(x in '0123456789abcdef' for x in password)
 statement="SET log_statement='none'; DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='crawl_cdc_validation') THEN CREATE ROLE crawl_cdc_validation LOGIN REPLICATION; END IF; END $$;\n"
 statement+="ALTER ROLE crawl_cdc_validation WITH PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT REPLICATION CONNECTION LIMIT 5;\n"
 statement+=(c.ROOT/'ops/cdc/validation-schema.sql').read_text()
 c.sql(c.leader(),statement,'crawler')
 tables=c.sql(c.leader(),"SELECT schemaname||'.'||tablename FROM pg_publication_tables WHERE pubname='crawl_cdc_validation' ORDER BY 1;",'crawler')
 assert tables=='cdc_validation.outbox', 'Publication scope expanded unexpectedly'
 assert c.sql(c.leader(),"SELECT puballtables FROM pg_publication WHERE pubname='crawl_cdc_validation';",'crawler')=='f'
 sources=[*c.NODES.values(),'10.4.4.12','10.4.4.3','10.4.4.17']
 for ip in c.NODES.values():
  hba='/etc/postgresql/17/main/pg_hba.conf';text=c.run(ip,'sudo cat '+hba,capture=True).stdout
  if '# CDC validation role' not in text:
   rules='# CDC validation role: only crawler through private nodes\n'+''.join('hostssl crawler crawl_cdc_validation '+source+'/32 scram-sha-256\n' for source in sources)
   rules+='host all crawl_cdc_validation 0.0.0.0/0 reject\nhost all crawl_cdc_validation ::0/0 reject\n'
   c.put(ip,hba,rules+text,'postgres',0o640)
  c.run(ip,'sudo -u postgres psql -XAt -c "SELECT pg_reload_conf();"',capture=True)
 secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'cdc-validation-db','namespace':'crawl-validation'},'type':'Opaque','stringData':{'database.properties':'password='+password+'\n'}}
 subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(secret),text=True,check=True,capture_output=True)
 print('Restricted role, exact single-table publication and protected K8s secret prepared')
if __name__=='__main__':main()
