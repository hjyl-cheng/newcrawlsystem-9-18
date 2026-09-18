# 基础设施复核与 GitOps 验收

日期：2026-09-18

## 存储服务复核

| 服务 | 实测结果 |
|---|---|
| PostgreSQL | S1 Primary；S2/S3 streaming、async，检查时 WAL replay 差距为 0 |
| PgBouncer | S2 经 S1:6432 使用 crawler 账号连接 crawler 数据库成功 |
| Kafka | 修复目录权限后恢复三 voter quorum，MaxFollowerLag=0；Publication 三分区 ISR 均含 1/2/3 |
| Kafka 读写 | 独立临时三副本 Topic，acks=all 生产，经 S2 消费成功；测试 Topic 已删除 |
| SeaweedFS | 三 Master 同意同一个 Leader；1 MiB S3 对象上传、读取 SHA-256 一致，测试对象和 Bucket 已删除 |
| ClickHouse | S3 服务已完成认证、持久化和内网读写验证，见 clickhouse 检查记录 |

发现并修复：先前 SeaweedFS 脚本将共享父目录 `/srv/crawlsystem` 改为 root:seaweedfs 0750，
导致 Kafka 用户无法遍历目录，出现 AccessDeniedException 和重启循环。
三台共享父目录恢复 root:root 0755，SeaweedFS 私有子目录维持自身权限；修正已进入 Git。
Kafka 数据及 cluster id 未重新初始化。

## Kubernetes

- A1/A2/A3 均 Ready，三个 etcd endpoint 提案健康检查通过。
- Calico VXLAN Always、IPIP Never，内网节点地址和 VXLAN 路由存在。
- Argo 管理的基础设施 DaemonSet 在 A1/A2/A3 各运行一个 Pod。
- 三个 Pod 均可访问 S1:5432/6432/9092、S2:9092、S3:9092/8333/8123/9000。
- 同节点 Pod HTTP 返回正确版本。

### 跨节点验收（修复后通过）

初次实测六个方向的跨节点 Pod HTTP 均超时。
A1 在 eth0 抓包看到发往 A2 的 VXLAN UDP 4789 重传，未看到来自 A2 的对应报文。
节点 Ready、Calico Ready 和 Argo Healthy 不能代替网络流量实测。

用户已补充并应用轻量云入站规则：

| 协议 | 目标端口 | 来源 | 应用实例 |
|---|---|---|---|
| UDP | 4789 | 10.4.4.0/22 | A1 43.173.68.88、A2 43.173.68.187、A3 43.172.94.2 |

规则生效后运行 `python3 ops/scripts/check-cluster-network.py`，退出码为 0：
三个 Pod 之间全部九组 HTTP（含六组跨节点）通过；每个节点的集群 DNS、ClusterIP Service、存储端口检查均通过。
各节点返回 `crawlsystem-infra-validation-v1`，也确认了 Git 回滚后的运行内容。

## GitOps

- 唯一来源：新仓库 `hjyl-cheng/newcrawlsystem-9-18` 的 main。
- 使用公开仓库 HTTPS 只读拉取；旧仓库未作为 remote 或 Argo 源。
- AppProject：crawl-validation，只允许验证命名空间中的 ConfigMap、Service、DaemonSet。
- Application：crawl-infra-validation，自动同步、selfHeal、prune。
- 验证入口只在集群内，不对公网开放；没有部署业务服务或业务镜像 CI。
- v1 初次发布：38648ed；三个节点返回 v1。
- v2 发布：8091ec6；Synced / Healthy，三个节点返回 v2。
- Git revert 回滚提交：6e1ef88；Synced / Healthy，三个节点恢复 v1，Argo history 记录三个部署 revision。

操作说明见 `deploy/argocd/README.md`。

## 仍属于验证环境的边界

API 地址仍依赖 A1；Argo CD 组件目前集中于 A1。
PG 异步物理复制、单 S1 PgBouncer 尚未完成自动切主、稳定写入口和备份恢复演练。
SeaweedFS 的 Filer/S3 仅在 S3，当前对象副本策略和元数据 HA 尚未验收。
ClickHouse 为 S3 单节点。不能把本次服务存活与发布验证称为生产 HA 完成。
