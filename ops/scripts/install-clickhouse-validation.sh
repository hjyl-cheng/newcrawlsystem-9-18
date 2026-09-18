#!/usr/bin/env bash
set -euo pipefail
# Password is supplied through a local file and copied separately; never committed.
source "${CRAWL_CH_SECRET_FILE:?set CRAWL_CH_SECRET_FILE}"
: "${CLICKHOUSE_PASSWORD:?missing password}"
CH_PASSWORD_HASH=$(printf '%s' "$CLICKHOUSE_PASSWORD" | sha256sum | cut -d ' ' -f1)
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg
sudo install -d -m 0755 /usr/share/keyrings
if [[ ! -s /usr/share/keyrings/clickhouse-keyring.gpg ]]; then
  curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor | sudo tee /usr/share/keyrings/clickhouse-keyring.gpg >/dev/null
fi
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main' | sudo tee /etc/apt/sources.list.d/clickhouse.list >/dev/null
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq clickhouse-server=26.8.6.5 clickhouse-client=26.8.6.5 clickhouse-common-static=26.8.6.5
sudo install -d -m 0755 /etc/clickhouse-server/config.d
sudo tee /etc/clickhouse-server/config.d/99-crawlsystem.xml >/dev/null <<'XML'
<clickhouse>
    <listen_host replace="replace">127.0.0.1</listen_host>
    <listen_host>10.4.4.5</listen_host>
    <max_server_memory_usage_to_ram_ratio>0.30</max_server_memory_usage_to_ram_ratio>
    <max_concurrent_queries>20</max_concurrent_queries>
    <background_pool_size>16</background_pool_size>
    <background_schedule_pool_size>4</background_schedule_pool_size>
</clickhouse>
XML
sudo chown root:root /etc/clickhouse-server/config.d/99-crawlsystem.xml
sudo install -d -m 0750 -o root -g clickhouse /etc/clickhouse-server/users.d
sudo tee /etc/clickhouse-server/users.d/crawlsystem.xml >/dev/null <<XML
<clickhouse>
  <profiles><validation><max_threads>2</max_threads><max_memory_usage>536870912</max_memory_usage></validation></profiles>
  <users>
    <default><networks replace="replace"><ip>127.0.0.1</ip><ip>::1</ip></networks></default>
    <crawler_validation>
      <password_sha256_hex>$CH_PASSWORD_HASH</password_sha256_hex>
      <networks><ip>10.4.4.0/22</ip><ip>127.0.0.1</ip></networks>
      <profile>validation</profile><quota>default</quota>
      <allow_databases><database>crawler_analytics</database></allow_databases>
    </crawler_validation>
  </users>
</clickhouse>
XML
sudo chown root:clickhouse /etc/clickhouse-server/users.d/crawlsystem.xml
sudo chmod 0640 /etc/clickhouse-server/users.d/crawlsystem.xml
sudo systemctl enable clickhouse-server
sudo systemctl restart clickhouse-server
sudo systemctl is-active clickhouse-server
clickhouse-client --host 127.0.0.1 --query 'CREATE DATABASE IF NOT EXISTS crawler_analytics'
clickhouse-client --host 127.0.0.1 --query 'SELECT version()'
