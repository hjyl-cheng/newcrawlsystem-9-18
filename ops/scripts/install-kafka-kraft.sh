#!/usr/bin/env bash
set -euo pipefail
NODE_ID="${1:?usage: $0 NODE_ID NODE_IP CLUSTER_ID}"
NODE_IP="${2:?usage: $0 NODE_ID NODE_IP CLUSTER_ID}"
CLUSTER_ID="${3:?usage: $0 NODE_ID NODE_IP CLUSTER_ID}"
case "$NODE_ID" in 1|2|3) ;; *) echo 'NODE_ID must be 1, 2, or 3' >&2; exit 2;; esac

# Bootstrap only: never overwrite a running cluster's TLS/ACL or storage identity.
if sudo test -e /etc/kafka/server.properties || sudo test -e /srv/crawlsystem/kafka/logs/meta.properties; then
  echo 'Existing Kafka configuration/storage found; use the reviewed rolling migration procedure.' >&2
  exit 1
fi

KAFKA_VERSION=3.9.1
KAFKA_HOME=/opt/kafka
KAFKA_ARCHIVE="kafka_2.13-${KAFKA_VERSION}.tgz"
KAFKA_URL="https://repo.huaweicloud.com/apache/kafka/${KAFKA_VERSION}/${KAFKA_ARCHIVE}"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq openjdk-17-jre-headless curl ca-certificates
if ! id kafka >/dev/null 2>&1; then sudo useradd --system --home-dir /var/lib/kafka --create-home --shell /usr/sbin/nologin kafka; fi
sudo install -d -m 0755 /opt /etc/kafka /srv/crawlsystem/kafka/logs
if [[ ! -x /opt/kafka/bin/kafka-server-start.sh ]]; then
  sudo curl -fsSL "$KAFKA_URL" -o "/tmp/$KAFKA_ARCHIVE"
  sudo tar -xzf "/tmp/$KAFKA_ARCHIVE" -C /opt
  sudo ln -sfn "/opt/kafka_2.13-${KAFKA_VERSION}" "$KAFKA_HOME"
  sudo rm -f "/tmp/$KAFKA_ARCHIVE"
fi
sudo chown -R kafka:kafka "/opt/kafka_2.13-${KAFKA_VERSION}" /var/lib/kafka /srv/crawlsystem/kafka
sudo tee /etc/kafka/server.properties >/dev/null <<CONF
process.roles=broker,controller
node.id=$NODE_ID
controller.quorum.voters=1@10.4.4.2:9093,2@10.4.4.8:9093,3@10.4.4.5:9093
listeners=PLAINTEXT://$NODE_IP:9092,CONTROLLER://$NODE_IP:9093
advertised.listeners=PLAINTEXT://$NODE_IP:9092
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
inter.broker.listener.name=PLAINTEXT
controller.listener.names=CONTROLLER
num.network.threads=2
num.io.threads=4
socket.send.buffer.bytes=102400
socket.receive.buffer.bytes=102400
socket.request.max.bytes=104857600
log.dirs=/srv/crawlsystem/kafka/logs
num.partitions=3
default.replication.factor=3
min.insync.replicas=2
offsets.topic.replication.factor=3
transaction.state.log.replication.factor=3
transaction.state.log.min.isr=2
group.initial.rebalance.delay.ms=0
log.retention.hours=168
log.segment.bytes=1073741824
CONF
sudo chown kafka:kafka /etc/kafka/server.properties
sudo chmod 0640 /etc/kafka/server.properties
sudo -u kafka /opt/kafka/bin/kafka-storage.sh format --config /etc/kafka/server.properties --cluster-id "$CLUSTER_ID" --ignore-formatted
sudo tee /etc/systemd/system/kafka.service >/dev/null <<'UNIT'
[Unit]
Description=Apache Kafka KRaft broker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=kafka
Group=kafka
Environment="KAFKA_HEAP_OPTS=-Xms256m -Xmx512m"
ExecStart=/opt/kafka/bin/kafka-server-start.sh /etc/kafka/server.properties
KillMode=mixed
TimeoutStopSec=120
SuccessExitStatus=143
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now kafka
sudo systemctl is-active kafka
