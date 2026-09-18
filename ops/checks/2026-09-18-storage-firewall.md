# S 节点数据面防火墙

日期：2026-09-18

## 历史阻断与当前状态

初次安装时 S2 到 S1 的 TCP `5432` 超时。后续规则已放通，PG 主备、Kafka、SeaweedFS、ClickHouse 和 PgBouncer 的基础验收已通过，最新证据见 `2026-09-18-infrastructure-gitops.md`。

## 轻量云防火墙规则

将以下规则以源网段 `10.4.4.0/22` 应用到 S1/S2/S3。所有端口只允许内网访问：

| 协议 | 端口 | 用途 |
|---|---:|---|
| TCP | 5432 | PostgreSQL Primary/Standby |
| TCP | 9092-9093 | Kafka Broker/Controller |
| TCP | 9333 | SeaweedFS Master |
| TCP | 19333 | SeaweedFS Master gRPC |
| TCP | 8080 | SeaweedFS Volume HTTP |
| TCP | 18080 | SeaweedFS Volume gRPC |
| TCP | 8888 | SeaweedFS Filer HTTP |
| TCP | 8333 | SeaweedFS S3 API |
| TCP | 8123 | ClickHouse HTTP |
| TCP | 9000 | ClickHouse Native |
| TCP | 6432 | PgBouncer |

不应把这些数据面端口开放到公网。

## 验收命令

```bash
# 在 S2 上执行
timeout 4 bash -c '</dev/tcp/10.4.4.2/5432'

# 在当前工作目录执行（需 kubectl 管理权限）
python3 ops/scripts/check-cluster-network.py
```

当前 PostgreSQL 主备安装脚本已设置 `PGCONNECT_TIMEOUT=10`，防止端口未放行时无限等待。
