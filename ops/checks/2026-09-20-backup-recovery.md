# 外围第一项：PostgreSQL 与集群配置备份恢复

日期：2026-09-20。依据用户最新要求，暂停业务代码推进，本轮只实施基础设施配置、运维脚本和真实恢复验收。

## PostgreSQL 已完成

- S1/S2/S3 安装 pgBackRest 2.59.1，S2 为专用仓库；AES-256-CBC 加密，私有目录/配置和受限 SSH remote 命令。
- S1 从 archive_mode=off 改为 on，归档目标为 S2；archive_timeout=300s。短暂重启主库后，S2/S3 均重新 streaming/async，现有 Data Ingestor 和 Temporal Ready。
- `pgbackrest check` 确认真正的 WAL 文件进入远端仓库；复查 archived_count=10、failed_count=0，PG 配置解析错误数为 0（该计数是当次观察，不是永久指标）。
- 首份完整备份 `20260920-172412F`：源数据 64.5 MB、2855 文件，仓库约 9 MB。
- 实际通过 systemd 执行差异备份 `20260920-172412F_20260920-172656D`：本次差异源约 2.6 MB，仓库增量约 404.5 KB。
- S2 两个 timer 已启用：周日全备、周一至周六差异备份；保留 2 个完整备份、6 个差异备份，WAL 随保留策略管理。

## PG 独立恢复证据

- 脚本 `ops/backup/verify-pg-restore.py`，使用首份完整备份与随后归档 WAL。
- 在完整备份之后建立唯一测试 schema：写入 before → 建立命名恢复点 → 写入 after → 强制归档。
- 命名恢复点：`crawl_restore_3d367e209c884729`。
- S3 独立目录：`/srv/crawlsystem/restore-check-3d367e209c884729`。只开该目录的私有 Unix socket，不监听 TCP，不使用原备库的端口/目录/复制槽。
- 恢复后 before 存在、after 不存在，证明归档日志实际重放并停在指定点；入库回执 ID、Submission ID 和内容 Hash 与恢复点前读取一致。
- crawler、独立验证库、两个 Temporal 库等均存在；没有把恢复范围局限于空库。
- 演练实例已停止，原 S3 备库继续正常复制。主库本轮唯一测试 schema 已清理；独立恢复目录保留 0700 供核查。

## Kubernetes 与配置备份已完成

- A1 使用固定 etcd 3.5.24 镜像生成一致快照；六节点关键服务配置、集群 PKI 和运维凭据形成 manifest，整体 GPG 加密后复制到 S2，并核对密文 SHA-256。
- 首次成功档案：`control-20260920T093049Z.tar.gpg`，约 2 MB。密文位于 A1/S2 的 root 私有目录；口令在 A1/S3 root 私有目录留存，不进 Git。
- 首份 etcd 快照 revision=338271、totalKey=1605、totalSize=12574720 字节。快照内部历史键数量与当前 registry 对象数含义不同。
- 对该档案解密后，manifest 8 个文件校验全部一致；通过 etcdutl 在临时目录恢复，再启动 127.0.0.1:12379/12380 的独立 etcd。
- 恢复实例可读取 registry 当前对象 570、Secrets 10、Deployments 11，仅检查数量，未输出秘密键值。
- 演练容器和明文临时目录已清理，原 Kubernetes etcd 成员/数据目录未变动。
- A1 每天 03:10 的 `crawl-control-backup.timer` 已启用，随机延迟最多 5 分钟，两地各保留 14 份完成档案。
- 首次执行发现 A2/A3 尚未配置基础设施公钥；通过已有受信账号补齐公钥后，systemd 实际运行成功。没有关闭主机身份检查或启用 root SSH。

## 现场健康与实际边界

三个 A 节点 Ready，四个 Argo Application 均 Synced/Healthy；Data Ingestor、Temporal、runtime-smoke 均 2/2 Ready。S1 的 PG/Kafka/SeaweedFS/PgBouncer 服务 active，S2/S3 主备复制保持 streaming。

本轮脚本语法检查和 `git diff --check` 通过，实际完整备份、差异备份、PG 指定点恢复、加密档案解密及 etcd 独立启动均通过。没有修改业务模块，因此没有重复无关的业务单元测试。

仍未完成：外部/异地备份及独立密钥保管、备份监控告警、整套集群重建演练、PG 自动切主和稳定入口、Kafka/SeaweedFS/ClickHouse 数据恢复验收。PG data_checksums 原为 off，本轮未改；PG 备份配置仍绑定 S1 主库，接 HA 时需要一并调整。

当前备份在同地域、同账号的现有系统盘，跨机不等于异地灾备。此次 etcd 恢复没有模拟所有控制面丢失后的证书/成员/入口恢复，不宣称完成生产 RPO/RTO。

配置与维护见 `ops/backup/README.md`；整体后续顺序见根目录 24.12。
