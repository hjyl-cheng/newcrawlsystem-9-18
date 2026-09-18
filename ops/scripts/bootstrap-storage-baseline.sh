#!/usr/bin/env bash
set -euo pipefail

NODE_NAME="${1:?usage: $0 s1|s2|s3}"
case "$NODE_NAME" in
  s1|s2|s3) ;;
  *) echo "unsupported node: $NODE_NAME" >&2; exit 2 ;;
esac

export DEBIAN_FRONTEND=noninteractive
sudo hostnamectl set-hostname "$NODE_NAME"
sudo apt-get update -qq
sudo apt-get install -y -qq ca-certificates curl jq rsync lsof xfsprogs chrony python3

# The validation topology keeps swap disabled. Keep fstab from re-enabling it.
sudo swapoff -a
sudo sed -ri '/^[^#].*[[:space:]]swap[[:space:]]/ s/^/# disabled for crawlsystem: /' /etc/fstab

# Stable private-name resolution for all six nodes.
sudo install -d -m 0755 /etc/crawlsystem
sudo tee /etc/crawlsystem/hosts >/dev/null <<'HOSTS'
10.4.4.12 a1
10.4.4.3  a2
10.4.4.17 a3
10.4.4.2  s1
10.4.4.8  s2
10.4.4.5  s3
HOSTS
if ! grep -q '^# BEGIN crawlsystem nodes$' /etc/hosts; then
  sudo tee -a /etc/hosts >/dev/null <<'HOSTS'
# BEGIN crawlsystem nodes
10.4.4.12 a1
10.4.4.3  a2
10.4.4.17 a3
10.4.4.2  s1
10.4.4.8  s2
10.4.4.5  s3
# END crawlsystem nodes
HOSTS
fi

# Shared service defaults. Component-specific limits are applied by each service unit.
sudo tee /etc/sysctl.d/90-crawlsystem-storage.conf >/dev/null <<'SYSCTL'
vm.max_map_count=262144
fs.file-max=2097152
net.core.somaxconn=4096
SYSCTL
sudo sysctl --system >/dev/null
sudo tee /etc/security/limits.d/crawlsystem-storage.conf >/dev/null <<'LIMITS'
* soft nofile 1048576
* hard nofile 1048576
LIMITS

# Keep data paths stable so later disk migration does not change service configuration.
sudo install -d -m 0750 /srv/crawlsystem/{postgres,kafka,seaweedfs,clickhouse,backup}
sudo install -d -m 0750 /var/log/crawlsystem
sudo systemctl enable --now chrony

printf 'node=%s\n' "$NODE_NAME"
printf 'hostname=%s\n' "$(hostname)"
printf 'swap_lines=%s\n' "$(swapon --show --noheadings | wc -l)"
printf 'map_count=%s\n' "$(sysctl -n vm.max_map_count)"
printf 'data_root=%s\n' "$(df -hP /srv/crawlsystem | tail -1)"
