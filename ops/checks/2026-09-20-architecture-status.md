# 整体架构实施状态核对

核对日期：2026-09-20。依据：当前仓库代码、已有验收记录，以及本轮对 Kubernetes 和 S1/S2/S3 的只读检查。

结论：六台基础设施与 GitOps 发布通路已建立；Kafka Results → Data Ingestor → PostgreSQL 的增量指标验证链路已运行。真实任务调度、真实采集 Worker 和完整业务闭环尚未接通。整体仍在 24.5 第 8 步的局部业务切片阶段，不能视为生产平台已完成。

## 当前实际部署

| 服务器 | 内网 IP | 本轮确认的运行组件 |
|---|---|---|
| A1 | 10.4.4.12 | Kubernetes 控制面/etcd、Calico；Argo CD 各组件；1 个 Data Ingestor 副本；基础网络探针 |
| A2 | 10.4.4.3 | Kubernetes 控制面/etcd、Calico；1 个 runtime-smoke 副本；基础网络探针 |
| A3 | 10.4.4.17 | Kubernetes 控制面/etcd、Calico；1 个 Data Ingestor 副本；1 个 runtime-smoke 副本；基础网络探针 |
| S1 | 10.4.4.2 | PostgreSQL Primary、PgBouncer、Kafka Broker/Controller、SeaweedFS Master/Volume |
| S2 | 10.4.4.8 | PostgreSQL Standby、Kafka Broker/Controller、SeaweedFS Master/Volume |
| S3 | 10.4.4.5 | PostgreSQL Standby、Kafka Broker/Controller、SeaweedFS Master/Volume/Filer/S3 Gateway、单节点 ClickHouse |

Pod 所在节点是本次观察值，未来调度可能变化。runtime-smoke 和 infra-smoke 是发布/网络验证程序，不是采集 Worker 或业务控制台。

## 实时检查证据

- A1/A2/A3 均 Ready；三个 Argo Application 均 Synced/Healthy，当前同步提交为 `9e36a90903cc2255f6ed6c598655b213bfdc0c89`。
- Data Ingestor 2/2 Ready，当前 Pod 零重启；一个副本的 `/health/ready` 返回 200/ready，`/version` 为源码提交 `6255a8832b05d381cec469c3841a5c121b3bd650`。
- S1/S2/S3 上表列出的数据服务均为 active/running。S1 `pg_is_in_recovery()` 为 false；S2/S3 为 true。S1 显示两条复制连接均为 streaming/async。
- Kafka KRaft 三 voter 在线，本次 Leader 为 1、MaxFollowerLag=0。Results Topic 为 3 分区、3 副本、min ISR=2；每个分区 ISR 均包含三台节点。
- Results 消费组各分区 current offset/log end offset 均为 51/51，lag=0。没有新增采集任务，因此零积压不代表吞吐能力验收。
- 常驻验证库保留 6 条业务回执；消息处理记录 APPLIED=147、QUARANTINED=6，与此前重放验收一致。重复处理记录不等于新增内容数。
- 正式 `crawler` 数据库查询未发现用户表；本批建表和服务写入位于隔离验证库。
- `temporal-schema-1-32-0` 初始化 Job 已完成；Temporal 的两个验证数据库已存在，但没有 Temporal Server Deployment/StatefulSet，也没有采集 Worker 部署。
- 集群没有 StorageClass/PVC。

本轮没有发送 Kafka 消息、运行数据库写入测试、重启服务或变更部署。SeaweedFS/ClickHouse 本轮确认服务运行；其读写能力引用 2026-09-18 的验收记录，未重新执行写入探针。

## 已编写但未部署的采集模块

- `services/collector-runtime`：原版 Crawlee 会话管理、IP 健康池/探活/冷却/分配、请求适配。
- `services/collection`：采集层有限重试、YouTube.js fetch 接入、选中存量视频的数量指标映射、冻结 Submission 组装。
- 最近一次本地验收记录为 `npm test` 50 项通过，见 `2026-09-20-youtube-metrics.md`；本轮未重复运行测试。
- 测试使用本机代理和合成 YouTube 响应；没有真实代理或真实 YouTube 成功率证据。
- 这些模块和部分文档、Temporal 准备文件仍在本地未提交，不能将当前集群镜像视为包含这些新功能。

## 尚未接通的部分

```text
业务控制台 / Planner                 未实现
          |
          v
Temporal 调度服务                   仅初始化数据库，服务未部署
          |
          v
真实采集 Worker                     公共模块和指标切片本地完成，未部署
          |
          v
本地 Journal / 恢复                 未实现，存储选型未定
          |
          v
Kafka Results                       已运行并验证
          |
          v
Data Ingestor                       已部署两个副本
          |
          v
PostgreSQL 验证库 + APPLIED 查询     已运行并验证
          |
          +--> Publication / Debezium / 外部 Business 投递  尚未接通
          +--> 分析投递至 ClickHouse                      尚未接通
          +--> Projector / Read Model / 业务控制台         尚未接通
```

现有回执查询是 Data Ingestor 的局部接口，不等于完整 Query Engine。SeaweedFS 和 ClickHouse 服务已搭建，不等于其业务数据流已接入。全量/发现/新增评论正文/Agent 等执行角色、完整计划终态结算仍待实施。

## 已确认的架构调整与边界

- 入口按 24.9 使用 Worker → Kafka Results → Data Ingestor → PG。
- 代理按 24.10 使用原版 Crawlee + IP 生命周期扩展 + YouTube.js，不部署 Rota/Resin；目前代理组仅为进程内实现，跨节点配置分发和管理 UI 待做。
- 本地 Journal 在原方案中有恢复职责，但 SQLite 仅被提出作为候选，未定案、未编写相关代码、未部署；本轮不新增该选型。
- PostgreSQL 仍为单逻辑采集库方案；隔离测试库和 Temporal 服务库不代表对业务数据分片。外部 Business 数据库保持不变。
- PG 自动切主/稳定入口、Kubernetes API 稳定 HA 入口、Argo CD HA、SeaweedFS Filer 入口 HA、ClickHouse HA、备份恢复验收、Kafka SASL/TLS/ACL、集中监控告警均不能视为已完成。

## 接下来建议的工作顺序（尚未执行）

1. 明确 Kafka 接收后与 PG APPLIED 前的恢复责任，再定 Journal 的最小实现和持久磁盘部署方式。
2. 完成 Temporal 服务、最小可信任务发放与 Worker 接入，保持同一提交身份恢复。
3. 使用获授权的真实代理和少量视频，验证任务 → 采集 → Kafka → PG → 回执 → 任务结束。
4. 在最小闭环通过后扩充采集角色、业务控制台/代理配置、Publication 和分析链路；生产 HA 与恢复逐项验收。
