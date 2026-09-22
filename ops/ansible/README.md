# Host configuration ownership

Ansible owns reviewed host-level configuration that is outside Kubernetes. Run playbooks from this directory so `ansible.cfg` and `inventory.yml` are selected:

```bash
cd ops/ansible
ansible-playbook playbooks/container-runtime.yml --check
ansible-playbook playbooks/container-runtime.yml
```

`playbooks/container-runtime.yml` manages the official containerd and runc binaries on A1-A3. Release URLs and SHA256 values are pinned in the playbook. It preserves `/etc/containerd/config.toml`, requires the existing CRI v3 configuration to use systemd cgroups, installs a systemd drop-in, and configures an explicit `crictl` endpoint. Changed nodes are processed one at a time and returned to scheduling even when validation fails.

`playbooks/control-backup.yml` manages A1's encrypted control-plane backup program, systemd timer, and the official etcd tools used for online snapshots and isolated restores. The backup path does not depend on `ctr` or the Kubernetes workload runtime CLI.

Do not use this playbook to skip containerd minor releases. A future 2.x minor upgrade must follow the upstream supported sequence and pass the same per-node and whole-cluster gates recorded under `ops/checks/`.

`playbooks/cdc-guard.yml` only maintains the temporary CDC promotion guard. It remains until Pigsty/Patroni and the CDC failover path have an accepted mature replacement or an explicitly retained boundary.
