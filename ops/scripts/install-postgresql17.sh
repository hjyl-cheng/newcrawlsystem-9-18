#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:?usage: $0 primary|standby SLOT_NAME}"
SECRET_FILE="${CRAWL_PG_SECRET_FILE:?set CRAWL_PG_SECRET_FILE}"
PRIMARY_IP="${CRAWL_PG_PRIMARY_IP:-10.4.4.2}"

if [[ ! -r "$SECRET_FILE" ]]; then
  echo "secret file is not readable" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$SECRET_FILE"
: "${REPLICATION_PASSWORD:?missing REPLICATION_PASSWORD}"
: "${CRAWLER_PASSWORD:?missing CRAWLER_PASSWORD}"

export DEBIAN_FRONTEND=noninteractive
sudo install -d -m 0755 /usr/share/keyrings
if [[ ! -s /usr/share/keyrings/postgresql.gpg ]]; then
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor | sudo tee /usr/share/keyrings/postgresql.gpg >/dev/null
fi
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt noble-pgdg main' | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y -qq postgresql-17 postgresql-client-17

PG_CONF=/etc/postgresql/17/main
PG_DATA=/var/lib/postgresql/17/main
PG_SERVICE=postgresql@17-main

sudo systemctl stop "$PG_SERVICE" || true

# Keep the package-managed cluster and data path for validation. A production data disk
# migration can move this path without changing the endpoint or application contract.
if [[ "$ROLE" == primary ]]; then
  sudo tee "$PG_CONF/conf.d/99-crawlsystem.conf" >/dev/null <<'CONF'
listen_addresses = '*'
wal_level = replica
max_wal_senders = 10
max_replication_slots = 10
hot_standby = on
wal_keep_size = '512MB'
password_encryption = 'scram-sha-256'
max_connections = 200
shared_buffers = '512MB'
CONF
  if ! sudo grep -q '^# BEGIN crawlsystem hba$' "$PG_CONF/pg_hba.conf"; then
    sudo tee -a "$PG_CONF/pg_hba.conf" >/dev/null <<'HBA'

# BEGIN crawlsystem hba
host replication replicator 10.4.4.0/22 scram-sha-256
host all all 10.4.4.0/22 scram-sha-256
# END crawlsystem hba
HBA
  fi
  sudo systemctl start "$PG_SERVICE"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'replicator') THEN CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '$REPLICATION_PASSWORD'; ELSE ALTER ROLE replicator WITH REPLICATION LOGIN PASSWORD '$REPLICATION_PASSWORD'; END IF; END \$\$;"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crawler') THEN CREATE ROLE crawler WITH LOGIN PASSWORD '$CRAWLER_PASSWORD'; ELSE ALTER ROLE crawler WITH LOGIN PASSWORD '$CRAWLER_PASSWORD'; END IF; END \$\$;"
  if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='crawler'" | grep -qx 1; then
    sudo -u postgres createdb -O crawler crawler
  fi
  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "SELECT pg_create_physical_replication_slot(slot_name) FROM (VALUES ('s2'), ('s3')) AS v(slot_name) WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots r WHERE r.slot_name = v.slot_name);" >/dev/null
  sudo systemctl restart "$PG_SERVICE"
  sudo -u postgres psql -Atqc "SELECT version();"
  sudo -u postgres psql -Atqc "SELECT 'primary=' || pg_is_in_recovery();"
else
  SLOT_NAME="${2:?standby requires slot name (s2 or s3)}"
  case "$SLOT_NAME" in s2|s3) ;; *) echo "unsupported slot" >&2; exit 2;; esac
  sudo tee "$PG_CONF/conf.d/99-crawlsystem.conf" >/dev/null <<'CONF'
listen_addresses = '*'
hot_standby = on
password_encryption = 'scram-sha-256'
max_connections = 200
shared_buffers = '512MB'
CONF
  if ! sudo grep -q '^# BEGIN crawlsystem hba$' "$PG_CONF/pg_hba.conf"; then
    sudo tee -a "$PG_CONF/pg_hba.conf" >/dev/null <<'HBA'

# BEGIN crawlsystem hba
host all all 10.4.4.0/22 scram-sha-256
# END crawlsystem hba
HBA
  fi
  sudo install -m 0600 -o postgres -g postgres /dev/null /var/lib/postgresql/.pgpass
  sudo sh -c "printf '%s:%s:*:replicator:%s\\n' '$PRIMARY_IP' 5432 '$REPLICATION_PASSWORD' > /var/lib/postgresql/.pgpass"
  sudo chown postgres:postgres /var/lib/postgresql/.pgpass
  sudo chmod 0600 /var/lib/postgresql/.pgpass
  sudo find "$PG_DATA" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  sudo -u postgres env PGCONNECT_TIMEOUT=10 pg_basebackup -h "$PRIMARY_IP" -D "$PG_DATA" -U replicator -Fp -Xs -P -R -S "$SLOT_NAME" --slot="$SLOT_NAME" --write-recovery-conf
  sudo tee -a "$PG_CONF/conf.d/99-crawlsystem.conf" >/dev/null <<CONF
primary_conninfo = 'host=$PRIMARY_IP port=5432 user=replicator passfile=/var/lib/postgresql/.pgpass application_name=$SLOT_NAME'
primary_slot_name = '$SLOT_NAME'
CONF
  sudo chown -R postgres:postgres "$PG_DATA"
  sudo systemctl start "$PG_SERVICE"
  sudo -u postgres psql -Atqc "SELECT 'standby=' || pg_is_in_recovery();"
fi

sudo systemctl enable "$PG_SERVICE" >/dev/null
sudo systemctl is-active "$PG_SERVICE"
