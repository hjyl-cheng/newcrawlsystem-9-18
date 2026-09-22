# 外围备份与恢复运行手册

本目录只包含基础设施脚本，不开发采集业务。当前覆盖 PostgreSQL 全集群、Kubernetes etcd、六台机器关键配置和运维凭据。SeaweedFS 已另行完成小规模停写维护基线备份和独立服务恢复，见 `ops/seaweedfs/README.md`；其持续在线备份、Kafka/ClickHouse 的实际数据备份及异地灾备仍待完成。

## PostgreSQL

拓扑：S1/S2/S3 中由 Patroni 选出的当前主库 → pgBackRest 加密物理备份/WAL → S2 专用仓库；S3 可作为独立恢复演练位置。备份仓库自动发现三个 PG 节点中的主库。

- 工具固定为 `pgbackrest=2.59.1-1.pgdg24.04+1`，安装于 S1/S2/S3。不是新增数据库服务。
- 仓库 `/srv/crawlsystem/backups/pgbackrest`，S2 `pgbackrest` 系统用户、0700；三节点数据库端使用 postgres 用户。仓库配置 `/etc/pgbackrest/pgbackrest.conf` 归 pgbackrest；各 PG 端用 `/etc/pgbackrest/pg-node.conf`，归 postgres，均 0600。
- 仓库 AES-256-CBC 加密、gzip 压缩。随机口令和专用 SSH 密钥生成于忽略目录 `secrets/backup/`；禁止提交、打印或写入检查报告。
- SSH 密钥受来源 IP、restrict 和强制 pgBackRest remote 命令约束。主机密钥通过现有受信运维 SSH 读取，不关闭主机身份验证。
- 周日 03:30 全备，周一至周六 03:30 差异备份，随机延迟最多 5 分钟，时间为服务器 Asia/Shanghai。保留最近 2 个全备、6 个差异备份；WAL 由 pgBackRest 根据保留备份管理，不手动删除。
- 三节点由 Patroni 设置 `archive_mode=on`、`archive_command=pgbackrest --config=/etc/pgbackrest/pg-node.conf --stanza=crawler archive-push %p`、`archive_timeout=300s`。未设置会丢弃 WAL 的 archive queue 大小上限；归档失败时 WAL 会占用磁盘，应接后续告警。300 秒不是生产 RPO 承诺。
- 已按 `ops/postgresql-ha/prepare-backup-ha.py` 扩展主库发现、专用 SSH 方向和 S2 本地归档客户端；切主后的实际归档/备份证据见 PG HA 验收记录。S2 仓库仍是单仓库，S2 故障时依靠各主库 WAL 暂存，不能宣称备份目标本身 HA。
- PostgreSQL 原来的 data_checksums 为 off，本轮未离线重写开启。pgBackRest 文件校验和不等价于 PG 页校验。

历史首次配置（已接管集群不要重新执行；当前变更使用 `ops/postgresql-ha/` 手册）：三台先安装上述版本，再运行 `python3 ops/backup/bootstrap-pgbackrest.py`，在 S2 以 pgbackrest 用户执行 `pgbackrest --stanza=crawler stanza-create`。脚本本身不会重启数据库。

stanza 建立后，将 `pg-archive.conf` 安装为 S1 `/etc/postgresql/17/main/conf.d/98-crawl-archive.conf`，备份旧配置，检查配置并短暂重启验证主库。确认 archive_mode、两条复制连接和应用恢复后，在 S2 执行：

```bash
sudo -u pgbackrest pgbackrest --stanza=crawler check
sudo -u pgbackrest pgbackrest --stanza=crawler --type=full backup
sudo -u pgbackrest pgbackrest --stanza=crawler info
```

将 `crawl-pgbackrest-{full,diff}.{service,timer}` 安装到 S2 systemd，启用两个 timer。`systemctl start crawl-pgbackrest-diff.service` 可验证服务单元能真实执行，不只检查 timer 存在。

### 独立恢复验收

```bash
python3 ops/backup/verify-pg-restore.py
```

要求已有完整备份。脚本在主库 postgres 库新建唯一测试 schema，写 before、创建命名恢复点、写 after，强制归档；在 S3 全新 `restore-check-<随机ID>` 目录恢复指定备份并回放 WAL 到恢复点。只开私有 Unix socket，端口标识 55432，不监听 TCP，不连接原主库、不占用原复制槽、不写原备库目录。

验收 before 存在、after 不存在、入库回执 ID/提交 ID/Hash 一致，且各服务数据库齐全。最后停止测试实例并删除主库的唯一测试 schema；S3 恢复目录保持 0700，保留供核查，不作为长期运行服务。

如启动过程中失败，先依据输出中的**独立恢复目录**检查该目录的 postmaster.pid/日志；仅使用该目录的 `pg_ctl -D ... stop` 清理。不得停止 S3 的 `crawl-patroni` 或直接控制其 PGDATA，不得拿本脚本覆盖现有 PGDATA。核对恢复进程已退出后，可清理本次独立恢复目录。

## Kubernetes 与配置

- A1 的 `crawl-control-backup.timer` 每天 03:10 执行，随机延迟最多 5 分钟。
- root 服务执行 `/opt/crawlsystem/backup/backup-control.py`；源文件位于本仓库，更新后以 root-owned 0750 安装。不能让系统服务直接执行可被普通用户修改的脚本。
- 使用集群已固定的 etcd 3.5.24 镜像及 healthcheck 证书生成一致快照，不复制正在使用的 etcd 数据目录充当备份。
- 另对 PG 专用 etcd 创建一致快照，自动尝试三个成员；备份使用 root-owned `/opt/crawlsystem/backup/bin/` 固定版本工具和 `/etc/crawl-backup/pg-etcd-pki/` 管理员凭据，不执行普通用户可修改的二进制。
- 包含六节点服务配置、Kubernetes PKI/静态 Pod 清单/kubeconfig/kubelet 配置、必要数据库配置/密钥、操作者 kubeconfig/SSH 和本地 secrets。所有明文只在 0700 临时目录处理，完成后清理。明确排除 `/etc/kubernetes/tmp` 中 kubeadm 每次升级留下的历史 etcd 副本；当前一致快照已单独生成，重复打包这些历史目录只会让每日档案膨胀。
- 生成 manifest（原文件 SHA-256、etcd revision、Git revision），GPG AES256 加密，再复制密文到 S2，校验两边 SHA-256 后重命名完成。只有复制成功才执行保留清理，两地各保留最近 14 份完成文件。
- 密文位于 A1/S2 `/srv/crawlsystem/backups/control`（root 0700）。A1 的 `last-success.json` 提供后续监控读取的最近成功记录，不含凭据。
- 解密口令位于 A1/S3 的 `/etc/crawl-backup/control-cipher-pass`（root 0600），本机忽略目录也有一份。S3 是当前跨机密钥保管副本；仍需独立账号/地域的外部保管，不能只依赖这六台服务器。
- A2/A3 原来未授权基础设施 SSH key，本轮补齐与 S 节点相同的运维公钥访问；未启用 root SSH 或取消主机密钥检查。

运行与验证：

```bash
sudo systemctl start crawl-control-backup.service
sudo systemctl list-timers --all crawl-control-backup.timer
sudo python3 ops/backup/verify-control-restore.py
```

恢复验证先解密最新档案并检查所有 manifest 校验和，再通过 etcdutl 恢复到临时目录，在 127.0.0.1:12379/12380 启动独立单成员 etcd，检查 registry/Secrets/Deployments 的记录数量。不会打印键值或秘密，不修改原集群成员，不替换原数据目录。最后停止并删除临时容器/目录。

PG 专用 etcd 另外恢复到独立目录，在 loopback 12479/12480 启动，使用备份里的证书核对 Patroni 配置、键数量和认证仍开启。

该检查证明备份可解密、快照可启动且对象存在，不等于六台机器全部丢失后的完整集群重建演练。正式 etcd 灾难恢复还需按故障时版本重新制定成员列表、证书、revision bump/mark-compacted 和 API 恢复流程，不能照搬验证端口。

## 日常检查与回滚

1. 检查两类 backup 服务最后 Result、timer 下次时间和备份新鲜度。后续集中监控要覆盖备份失败、磁盘空间、pg_stat_archiver.failed_count、WAL 积压及备份年龄。
2. pgBackRest `info` 必须有有效完整备份；`check` 必须确认 WAL 归档。不以“脚本退出过一次”为长期健康证明。
3. 改服务配置前更新本仓库并记录实际部署版本。备份不放 Git；保留密钥与恢复手册。
4. 需暂停定时任务时停用对应 timer，保留备份文件。暂停备份不应顺手关闭 WAL 归档或删除 WAL；归档目标故障先修复连接/容量并评估积压。
5. 这些副本仍处于同地域/同账号/系统盘，不能证明异地灾备或独立磁盘故障隔离；在有外部存储后再扩展仓库与密钥保管。

PG 自动切主与固定入口见 `ops/postgresql-ha/README.md`。本目录不迁移生产数据、不接外部告警收件人，也不把其他外围待办标记为完成。
