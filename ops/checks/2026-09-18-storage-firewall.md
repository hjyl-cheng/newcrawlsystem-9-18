# S 节点数据面防火墙

日期：2026-09-18

## 当前发现

S1 PostgreSQL 17 Primary 已启动并监听 `10.4.4.2:5432`。S2 到 S1 的 TCP `5432` 连接超时，说明控制面防火墙规则已经生效，但数据面端口尚未加入轻量云防火墙模板。

## 轻量云防火墙规则

将以下规则以源网段 `10.4.4.0/22` 应用到 S1/S2/S3。所有端口只允许内网访问：

| 协议 | 端口 | 用途 |
|---|---:|---|
| TCP | 5432 | PostgreSQL Primary/Standby |
| TCP | 9092-9093 | Kafka Broker/Controller |
| TCP | 9333 | SeaweedFS Master |
| TCP | 8888 | SeaweedFS Volume |
| TCP | 8333 | SeaweedFS S3 API |
| TCP | 8123 | ClickHouse HTTP |
| TCP | 9000 | ClickHouse Native |
| TCP | 6432 | PgBouncer（后续） |

不应把这些数据面端口开放到公网。

## 验收命令

```bash
# 在 S2 上执行
timeout 4 bash -c '</dev/tcp/10.4.4.2/5432'

# 在任意一台管理机执行
kubectl ... # 业务服务接入前暂不使用
```

当前 PostgreSQL 主备安装脚本已设置 `PGCONNECT_TIMEOUT=10`，防止端口未放行时无限等待。
