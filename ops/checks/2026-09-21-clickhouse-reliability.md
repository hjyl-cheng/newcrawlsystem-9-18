# ClickHouse 原生备份与恢复验收

2026-09-21，外围建设；新仓库内维护，旧仓库未修改，业务代码未开发。

- 现场版本 26.8.6.5，S3 单机；`crawler_analytics` 无业务表。约 762 MiB 的原数据目录含 ClickHouse 系统日志，未将其冒充业务数据或纳入本轮分析库备份。
- 原生 backup Disk 已热加载；备份线程 2、IO 线程上限 4、带宽上限 16 MiB/s 实测生效。在线服务未重启。
- A1 调用原生 BACKUP，校验原生 ZIP，AES256 加密后保存 A1/S2；两份密文 SHA256 一致、解密后原文件 SHA256 一致。保存最近 7 份完成的备份。
- 首份有数据的探针归档 `clickhouse-20260921T015853Z-276f725d.zip.gpg`，7035 字节，SHA256 `e056402b0e7f9931b0ffd963ed5fa8f02fa782ad25a9acf0287d51ee9e03f920`。
- 使用隔离 ClickHouse 数据目录/配置/loopback 端口恢复，3 行探针完全一致；备份后第 4 行没有混入，在线 4 行未改变。临时表/实例/目录清理完成。
- 修改备份完成文件的原子发布顺序后，再次运行实际 systemd 服务通过；最新归档 `clickhouse-20260921T020019Z-60eb3443.zip.gpg`，此时探针表已经清理，备份是当前空分析库。它不替代前一份含数据归档的恢复证据。
- 每日 04:10 CST +0～5分钟随机延后定时器 enabled/active；只读健康检查通过，包含备份年龄、两份密文校验和上次任务状态。恢复点、数据规模、跨表一致性及调度单点边界见运行手册。
- 收尾六个 Argo 应用 Synced/Healthy，全部应用 Deployment 2/2；Kafka 只读巡检通过。无需新增防火墙端口。

原始证据：`2026-09-21-clickhouse-backup-recovery.json`、`2026-09-21-clickhouse-health.json`。运行手册：`ops/clickhouse/README.md`。

**与 24.4 §23.10 的现场差异：** S3 没有独立数据盘，目前 `/var/lib/clickhouse` 仍在系统盘上，未完成生产 IO/故障域隔离。只增加 CPU/内存不能消除该差异。单节点不具备自动故障接管；没有部署 Keeper/副本，也未实现 analytics outbox 的退避/背压/回填。当前可以验收“备份能恢复”，不能验收“分析层已高可用”或“外围全部完成”。

下一步按 24.12 推进 Kafka Connect/Debezium 基础设施及 PG17 logical failover slot 的配置与恢复验证；用独立运维探针验证传输语义，不提前开发业务 Publication。集中监控、认证/TLS、持续对象备份、Argo 整机失联自动接管仍是后续事项。
