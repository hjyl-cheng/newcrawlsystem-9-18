# PostgreSQL 主备与 PgBouncer 验证

日期：2026-09-18

## 当前拓扑

```text
S1 10.4.4.2  PostgreSQL 17.11 Primary + PgBouncer :6432
 │
 ├── physical replication slot s2 → S2 10.4.4.8 Standby
 └── physical replication slot s3 → S3 10.4.4.5 Standby
```

## 已验证

- S1、S2、S3 PostgreSQL 17.11 服务均为 active。
- S2、S3 `pg_is_in_recovery()` 均为 true。
- S1 `pg_stat_replication` 显示 S2/S3 均为 `streaming`。
- `s2`、`s3` 物理复制槽均为 active。
- 在 S1 写入临时探针记录后，S2/S3 均读取到；探针已清理。
- S1 本机通过 PgBouncer `10.4.4.2:6432` 连接 crawler 数据库成功。

## 当前限制

当前 PgBouncer 只部署在 S1，是验证期入口；S1 故障切换和稳定 VIP/HAProxy 尚未完成。S2 → S1 的 `6432` 仍需要轻量云防火墙内网入站规则，应用节点接入前必须验证该端口。

生产前还需补齐：

1. 独立数据盘和 PostgreSQL 数据目录迁移；
2. PG17 failover logical slot 与 Debezium 切主 Runbook；
3. PgBouncer/HAProxy/VIP 的切主入口；
4. 同步或异步复制策略及 RPO/RTO 演练。
