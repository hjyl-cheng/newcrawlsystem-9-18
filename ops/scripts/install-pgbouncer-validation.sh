#!/usr/bin/env bash
set -euo pipefail
SECRET_FILE="${CRAWL_PG_SECRET_FILE:?set CRAWL_PG_SECRET_FILE}"
# shellcheck disable=SC1090
source "$SECRET_FILE"
: "${CRAWLER_PASSWORD:?missing CRAWLER_PASSWORD}"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq pgbouncer postgresql-client-17
sudo install -d -m 0750 -o postgres -g postgres /etc/pgbouncer
sudo tee /etc/pgbouncer/pgbouncer.ini >/dev/null <<'CONF'
[databases]
crawler = host=127.0.0.1 port=5432 dbname=crawler

[pgbouncer]
listen_addr = 10.4.4.2
listen_port = 6432
unix_socket_dir = /var/run/postgresql
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
admin_users = postgres
pool_mode = transaction
max_client_conn = 200
default_pool_size = 20
reserve_pool_size = 5
server_reset_query = DISCARD ALL
ignore_startup_parameters = extra_float_digits
CONF
sudo tee /etc/pgbouncer/userlist.txt >/dev/null <<USERS
"crawler" "$CRAWLER_PASSWORD"
USERS
sudo chown postgres:postgres /etc/pgbouncer/pgbouncer.ini /etc/pgbouncer/userlist.txt
sudo chmod 0640 /etc/pgbouncer/pgbouncer.ini
sudo chmod 0600 /etc/pgbouncer/userlist.txt
sudo install -d -m 0755 /etc/systemd/system/pgbouncer.service.d
sudo tee /etc/systemd/system/pgbouncer.service.d/limits.conf >/dev/null <<'UNIT'
[Service]
LimitNOFILE=1048576
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now pgbouncer
sudo systemctl restart pgbouncer
sudo systemctl is-active pgbouncer
PGPASSWORD="$CRAWLER_PASSWORD" psql "host=10.4.4.2 port=6432 dbname=crawler user=crawler sslmode=disable" -Atc 'select current_database(), current_user, pg_is_in_recovery();'
