#!/usr/bin/env python3
"""Prepare dedicated log DB, per-node INSERT accounts and verified HTTPS."""
import argparse,hashlib,importlib.util,json,os,secrets,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('nodes',ROOT/'ops/monitoring/bootstrap-nodes.py');nodes=importlib.util.module_from_spec(s);s.loader.exec_module(nodes)
PRIVATE=ROOT/'secrets/logging'
def sql(statement):return nodes.run('s3','sudo clickhouse-client --multiquery',statement).stdout

def main():
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true',required=True);p.parse_args();os.umask(0o077);PRIVATE.mkdir(mode=0o700,exist_ok=True)
 def openssl(*args):subprocess.run(['openssl',*map(str,args)],check=True,capture_output=True)
 if not (PRIVATE/'ca.crt').exists():
  assert not (PRIVATE/'ca.key').exists()
  openssl('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',PRIVATE/'ca.key','-out',PRIVATE/'ca.crt','-days','1825','-subj','/CN=crawl-logging-ca','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
 if not (PRIVATE/'server.crt').exists():
  openssl('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',PRIVATE/'server.key','-out',PRIVATE/'server.csr','-subj','/CN=crawl-logs-s3')
  (PRIVATE/'server.ext').write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:10.4.4.5,IP:127.0.0.1,DNS:s3\n')
  openssl('x509','-req','-in',PRIVATE/'server.csr','-CA',PRIVATE/'ca.crt','-CAkey',PRIVATE/'ca.key','-CAcreateserial','-out',PRIVATE/'server.crt','-days','730','-sha256','-extfile',PRIVATE/'server.ext')
 accounts={}
 for user in [*['crawl_logs_'+n for n in nodes.NODES],'crawl_logs_grafana']:
  f=PRIVATE/(user+'.password')
  if not f.exists():f.write_text(secrets.token_hex(32))
  accounts[user]=f.read_text().strip()
 nodes.run('s3','sudo install -d -m 0750 -o clickhouse -g clickhouse /etc/clickhouse-server/logging-pki')
 for file in ['ca.crt','server.crt','server.key']:nodes.put('s3','/etc/clickhouse-server/logging-pki/'+file,(PRIVATE/file).read_text(),'clickhouse')
 config='''<clickhouse><https_port>8443</https_port><openSSL><server>
<certificateFile>/etc/clickhouse-server/logging-pki/server.crt</certificateFile>
<privateKeyFile>/etc/clickhouse-server/logging-pki/server.key</privateKeyFile>
<verificationMode>none</verificationMode><disableProtocols>sslv2,sslv3,tlsv1,tlsv1_1</disableProtocols>
<preferServerCiphers>true</preferServerCiphers></server></openSSL></clickhouse>'''
 nodes.put('s3','/etc/clickhouse-server/config.d/97-crawl-logging.xml',config,'clickhouse',0o640)
 sql((ROOT/'ops/logging/schema.sql').read_text())
 sql('ALTER TABLE crawler_logs.events MODIFY SETTING old_parts_lifetime=60;')
 profile='''<max_threads>1</max_threads><max_memory_usage>134217728</max_memory_usage><max_execution_time>10</max_execution_time><max_concurrent_queries_for_user>2</max_concurrent_queries_for_user>'''
 users='<clickhouse><profiles><crawl_log_writer>'+profile+'''<async_insert>0</async_insert></crawl_log_writer><crawl_log_reader>'''+profile+'''<readonly>1</readonly><max_rows_to_read>2000000</max_rows_to_read><max_bytes_to_read>134217728</max_bytes_to_read><max_result_rows>5000</max_result_rows><max_result_bytes>16777216</max_result_bytes><result_overflow_mode>break</result_overflow_mode></crawl_log_reader></profiles><users>'''
 for user,password in accounts.items():
  reader=user.endswith('grafana');allowed=[ip for n,ip in nodes.NODES.items() if n.startswith('a')] if reader else [nodes.NODES[user.removeprefix('crawl_logs_')]]
  users+='<'+user+'><password_sha256_hex>'+hashlib.sha256(password.encode()).hexdigest()+'</password_sha256_hex><networks>'+''.join('<ip>'+ip+'</ip>' for ip in allowed)+'</networks><profile>'+('crawl_log_reader' if reader else 'crawl_log_writer')+'</profile><quota>default</quota><grants><query>GRANT '+('SELECT' if reader else 'INSERT')+' ON crawler_logs.events</query></grants></'+user+'>'
 users+='</users></clickhouse>'
 nodes.put('s3','/etc/clickhouse-server/users.d/crawl-logging.xml',users,'clickhouse',0o640)
 sql('SYSTEM RELOAD CONFIG;')
 # New listeners may require a restart on some server builds; check first.
 ready=nodes.run('s3',"sudo ss -lnt '( sport = :8443 )'").stdout
 if ':8443' not in ready:nodes.run('s3','sudo systemctl restart clickhouse-server')
 print(sql("SELECT name FROM system.tables WHERE database='crawler_logs';"))
 secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'grafana-logs-private','namespace':'crawl-monitoring'},'type':'Opaque','stringData':{'CLICKHOUSE_LOGS_PASSWORD':accounts['crawl_logs_grafana'],'CLICKHOUSE_LOGS_CA':(PRIVATE/'ca.crt').read_text()}}
 subprocess.run(['kubectl','apply','-f','-'],input=json.dumps(secret),text=True,capture_output=True,check=True)
 print('Log schema, restricted accounts, HTTPS and Grafana Secret prepared.')
if __name__=='__main__':main()
