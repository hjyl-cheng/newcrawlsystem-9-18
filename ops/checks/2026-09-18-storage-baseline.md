# S 节点存储基础基线

日期：2026-09-18

## 节点

| 节点 | 内网地址 | 角色 | 基线结果 |
|---|---|---|---|
| S1 | 10.4.4.2 | PostgreSQL Primary、Kafka Broker、SeaweedFS | Ubuntu 24.04、2 vCPU、约 7.6 GiB、79 GiB 根盘、swap 关闭 |
| S2 | 10.4.4.8 | PostgreSQL Standby、Kafka Broker、SeaweedFS | Ubuntu 24.04、2 vCPU、约 7.6 GiB、79 GiB 根盘、swap 关闭 |
| S3 | 10.4.4.5 | PostgreSQL Standby/仲裁、Kafka Broker、SeaweedFS、ClickHouse | Ubuntu 24.04、2 vCPU、约 7.6 GiB、79 GiB 根盘、swap 关闭 |

## 已完成

- 三台节点已使用密钥 SSH 登录，后续不依赖密码自动化。
- 主机名为 `s1`、`s2`、`s3`，六台节点内网解析已加入 `/etc/hosts`。
- 安装基础工具：`curl`、`jq`、`rsync`、`lsof`、`xfsprogs`、`chrony`、`python3`。
- `swap` 保持关闭。
- 已设置 `vm.max_map_count=262144`、`fs.file-max=2097152`、`net.core.somaxconn=4096`。
- 已设置文件句柄基线 `nofile=1048576`。
- 已创建稳定数据目录：`/srv/crawlsystem/{postgres,kafka,seaweedfs,clickhouse,backup}`。

## 说明

当前三台机器只有系统盘，数据目录暂时位于根盘，供链路验证使用。生产前应增加独立数据盘，并将这些目录迁移到数据盘；应用配置只引用 `/srv/crawlsystem/...`，因此迁移不改变服务拓扑。

本基线不启动 PostgreSQL、Kafka、SeaweedFS 或 ClickHouse。下一步按 24.4 的责任边界先安装并验证单逻辑 PostgreSQL Primary/Standby，再接入 PgBouncer 和其余存储服务。
