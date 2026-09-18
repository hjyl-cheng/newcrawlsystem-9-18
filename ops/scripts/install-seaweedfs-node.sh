#!/usr/bin/env bash
set -euo pipefail
NODE_NAME="${1:?usage: $0 s1|s2|s3 NODE_IP}"
NODE_IP="${2:?usage: $0 s1|s2|s3 NODE_IP}"
case "$NODE_NAME" in s1|s2|s3) ;; *) echo unsupported node >&2; exit 2;; esac
MASTER_LIST='10.4.4.2:9333,10.4.4.8:9333,10.4.4.5:9333'
ARCHIVE=/tmp/seaweedfs-linux_amd64.tar.gz

if [[ ! -s "$ARCHIVE" ]]; then echo "missing $ARCHIVE" >&2; exit 2; fi
if ! id seaweedfs >/dev/null 2>&1; then sudo useradd --system --home-dir /var/lib/seaweedfs --create-home --shell /usr/sbin/nologin seaweedfs; fi
sudo install -d -m 0755 /opt/seaweedfs /etc/seaweedfs
if [[ ! -x /opt/seaweedfs/weed ]]; then
  sudo tar -xzf "$ARCHIVE" -C /opt/seaweedfs
fi
# Shared parent must remain traversable by Kafka and other service users.
# Each service owns only its own 0750 data directory.
sudo chown root:root /srv/crawlsystem
sudo chmod 0755 /srv/crawlsystem
sudo chown seaweedfs:seaweedfs /srv/crawlsystem/seaweedfs
sudo install -d -o seaweedfs -g seaweedfs -m 0750 \
  /srv/crawlsystem/seaweedfs/master \
  /srv/crawlsystem/seaweedfs/volume \
  /srv/crawlsystem/seaweedfs/filer
sudo chown -R seaweedfs:seaweedfs /opt/seaweedfs

sudo tee /etc/seaweedfs/common.env >/dev/null <<ENV
WEED=/opt/seaweedfs/weed
NODE_IP=$NODE_IP
MASTER_LIST=$MASTER_LIST
ENV
sudo chown root:seaweedfs /etc/seaweedfs/common.env
sudo chmod 0640 /etc/seaweedfs/common.env

sudo tee /etc/systemd/system/seaweedfs-master.service >/dev/null <<UNIT
[Unit]
Description=SeaweedFS Master
After=network-online.target
Wants=network-online.target
[Service]
User=seaweedfs
Group=seaweedfs
ExecStart=/opt/seaweedfs/weed master -ip=$NODE_IP -ip.bind=$NODE_IP -port=9333 -mdir=/srv/crawlsystem/seaweedfs/master -peers=$MASTER_LIST
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/seaweedfs-volume.service >/dev/null <<UNIT
[Unit]
Description=SeaweedFS Volume
After=seaweedfs-master.service
Requires=seaweedfs-master.service
[Service]
User=seaweedfs
Group=seaweedfs
ExecStart=/opt/seaweedfs/weed volume -ip=$NODE_IP -ip.bind=$NODE_IP -port=8080 -dir=/srv/crawlsystem/seaweedfs/volume -mserver=$MASTER_LIST
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now seaweedfs-master
sudo systemctl enable --now seaweedfs-volume

if [[ "$NODE_NAME" == s3 ]]; then
  sudo tee /etc/systemd/system/seaweedfs-filer.service >/dev/null <<UNIT
[Unit]
Description=SeaweedFS Filer
After=seaweedfs-master.service
Requires=seaweedfs-master.service
[Service]
User=seaweedfs
Group=seaweedfs
ExecStart=/opt/seaweedfs/weed filer -ip=$NODE_IP -ip.bind=$NODE_IP -port=8888 -defaultStoreDir=/srv/crawlsystem/seaweedfs/filer -master=$MASTER_LIST
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT
  sudo tee /etc/systemd/system/seaweedfs-s3.service >/dev/null <<UNIT
[Unit]
Description=SeaweedFS S3 Gateway
After=seaweedfs-filer.service
Requires=seaweedfs-filer.service
[Service]
User=seaweedfs
Group=seaweedfs
ExecStart=/opt/seaweedfs/weed s3 -ip=$NODE_IP -ip.bind=$NODE_IP -port=8333 -filer=$NODE_IP:8888
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now seaweedfs-filer
  sudo systemctl enable --now seaweedfs-s3
fi
sudo systemctl is-active seaweedfs-master
sudo systemctl is-active seaweedfs-volume
if [[ "$NODE_NAME" == s3 ]]; then sudo systemctl is-active seaweedfs-filer seaweedfs-s3; fi
