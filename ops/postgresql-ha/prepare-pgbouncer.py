#!/usr/bin/env python3
"""Install equivalent small transaction pools on every potential PG primary."""
import runpy
from pathlib import Path
m = runpy.run_path(str(Path(__file__).with_name('prepare-patroni.py')))
root, run, put = (m[k] for k in ('ROOT','run','put'))
a = m['envfile'](root/'secrets/data-ingestor/runtime.env')
b = m['envfile'](root/'secrets/validation-storage.env')
users = [('crawler', b['CRAWLER_PASSWORD']), (a['PGUSER'], a['PGPASSWORD'])]
assert all('"' not in v and '\n' not in v and '\\' not in v for pair in users for v in pair)
for name, ip in m['NODES'].items():
    cfg = f'''[databases]
crawler = host=127.0.0.1 port=5432 dbname=crawler pool_size=4 reserve_pool_size=1
crawler_validation_ingestor = host=127.0.0.1 port=5432 dbname=crawler_validation_ingestor pool_size=8 reserve_pool_size=2

[pgbouncer]
listen_addr = {ip}
listen_port = 6432
unix_socket_dir = /var/run/postgresql
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
admin_users = postgres
pool_mode = transaction
max_client_conn = 200
default_pool_size = 8
reserve_pool_size = 2
max_prepared_statements = 100
server_reset_query = DISCARD ALL
server_connect_timeout = 5
server_login_retry = 2
query_wait_timeout = 30
server_idle_timeout = 60
ignore_startup_parameters = extra_float_digits
'''
    put(ip,'/etc/pgbouncer/pgbouncer.ini',cfg,'postgres',0o640)
    put(ip,'/etc/pgbouncer/userlist.txt',''.join(f'"{user}" "{password}"\n' for user,password in users),'postgres')
    run(ip,'sudo -n systemctl enable pgbouncer && sudo -n systemctl restart pgbouncer && sudo -n systemctl is-active pgbouncer')
    print(name+': transaction pool ready')
