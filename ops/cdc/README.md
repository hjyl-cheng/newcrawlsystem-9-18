# CDC 外围运行手册

2026-09-21。仅验证数据库变化到 Kafka 的传输底座，业务开发继续暂停。

## 现场部署

```text
S1/S2/S3 PostgreSQL 17.11 + Patroni
  当前 S1 主库；严格同步副本数 1
  crawler.cdc_validation.outbox（唯一被发布的探针表）
        │ pgoutput + failover logical slot
        ▼
A1/A2/A3 Kubernetes：kafka-connect Deployment，跨节点 2 个副本
  postgres-rw:5432 连接当前主库（不经过 PgBouncer 事务池）
  一个 PostgreSQL Connector / 一个任务，另一实例可承接任务
        │ Outbox Event Router
        ▼
S1/S2/S3 Kafka：crawler.publication.validation.v1
  3 分区，3 副本，min.insync.replicas=2
        │
        ▼
临时验收消费者：核对事件 ID、消息键、测试批次和收取数量
```

两个 Connect 副本是任务接管能力，不是同一 PG 槽的双任务并行解码。Pod 由 Kubernetes 调度，不固定每台 A 服务器一份。

- 镜像固定为 `quay.io/debezium/connect:3.6.3.Final@sha256:0e5e4792e278bc1409281985e4e53f02083467dc670c13c7af70b579487d48c6`。实际 REST 报告 Kafka Connect 4.3.0，Broker 是 3.9.1；本次组合实测通过，不以版本号相同作为依据。
- `deploy/base/kafka-connect/`、`deploy/overlays/validation/kafka-connect/` 由 Argo 应用 `crawl-kafka-connect-validation` 管理。只加载 PostgreSQL 插件组；插件路径必须指向包含插件子目录的父目录，不能将一组依赖 JAR 当成互相隔离的插件。
- REST 8083 仅集群内部；NetworkPolicy 限定 Connect 同伴和同 namespace 中 `cdc-admin=true` 的管理 Pod。标签不是身份认证，RBAC/集群管理员属于受信边界。未开放公网接口。
- 凭据在忽略目录 `secrets/cdc/` 和 Kubernetes Secret 中；配置引用 FileConfigProvider，不在 Git/Connector JSON 中保存明文。
- `crawl_cdc_validation` 是限连接数的 LOGIN/REPLICATION 角色，仅有 crawler 连接、探针 schema USAGE 和 outbox SELECT；不是超级用户。PG17 复制权限本身仍是敏感权限，需结合 HBA、publication、集群访问控制保护。
- Connect config/offset/status 三个内部 topic 都为 RF3/minISR2/compact；config 1 分区，offset/status 各 3 分区。结果 topic 保留 7 天且每分区 256 MiB，任一限制先到即可能删除旧段。heartbeat topic 为 1 分区 RF3/minISR2/compact。实际配置见验收 JSON。
- producer `acks=all`、幂等生产；不等于跨 PG/Kafka/业务消费的 exactly-once。消费者仍需事件 ID 去重。

## PG17 槽位配置与可用性边界

三节点 `wal_level=logical`、`hot_standby_feedback=on`、`sync_replication_slots=on`。复制连接已有物理槽及 `dbname=postgres`，HBA 允许三 S 节点 replicator 进行槽位同步所需的普通数据库连接。

`synchronized_standby_slots` 配置的是**物理备库槽名**：S1 为 `s2,s3`，S2 为 `s1,s3`，S3 为 `s1,s2`，不是逻辑槽名。逻辑槽 `crawl_cdc_validation` 开启 failover，Patroni 精确 ignore 此槽，由 PostgreSQL 原生同步，不混用两套槽位复制机制。

这是保守配置：CDC 等待两个指定备库接收 WAL，任一个离线时投递可能暂停；PG 普通写入仍采用严格同步 1 备库。尚未实现随安全晋升候选变化而调整等待集合的自动策略，不能宣称整机故障下 CDC 持续可用。计划切换前检查候选槽 `synced=true`、`temporary=false`、无 invalidation 且进度达到主库采样位置；同步槽进度存在刷新间隔。

`max_slot_wal_keep_size=2GB` 在检查点约束槽保留 WAL，**不是整个 pg_wal 目录的即时硬上限**。超限可能使槽失效；不能通过自动删槽/跳 LSN 换取“健康”。只读检查在保留 WAL 超 1 GiB 时报告异常，集中采集/告警尚未接入。长时间无被捕获表变更、其他库持续写入时的位点推进及告警，还需单独验收，不能仅凭配置了 heartbeat.interval.ms 判定解决。

## 使用与检查

以下准备步骤会变更数据库，已在现场执行，不应当作日常巡检重复运行：

1. `prepare-postgres.py --execute`：变更参数并滚动重启/必要切主；执行前先备份和检查同步副本。其 pre-cdc 文件是实施快照，不是长期配置版本库。
2. `prepare-probe.py --execute`：创建隔离探针 schema、精确 publication、角色及 Secret，不创建业务 Publication。
3. `node ops/cdc/provision-topics.mjs`：创建缺失 topic，校验分区和副本；已有 topic 的配置不会被自动修正，需另用 kafka-configs 查看实际值。
4. Git/Argo 部署 Connect 后运行 `python3 ops/cdc/connect-admin.py` 校验并应用 Connector 配置。Git 保存期望 JSON，目前由受控脚本提交 Connect REST；尚无 Connector 专用 GitOps 控制器，不声称自动纠偏。

日常只读检查：

```bash
python3 ops/cdc/check-health.py
node ops/kafka/check-health.mjs
kubectl -n argocd get applications
```

受控故障验证：`python3 ops/cdc/verify-chain.py --execute`。会写合成事件、删除任务所在 Pod、计划切主并尽量返回 S1；仅在维护窗口运行。它不是只读检查，不测试整机断电。完成或失败后检查主库、同步参数、槽和 Connect；删除临时管理 Pod：`kubectl -n crawl-validation delete pod cdc-admin-probe --ignore-not-found`。正常保留 Connector/槽用于后续验证；不要为清理探针随意删除槽。

## 旧主退为备库后的槽恢复

首次创建逻辑槽的主机 S3 退为备库时，原来 `synced=false` 的同名本地槽阻止原生同步。本轮按以下受控流程修复，不能记为无人值守恢复：

1. 确认问题节点只读，主库原槽 active/failover/非临时/未失效，主库确认位点不落后于旧槽。
2. 将问题节点 Patroni `nofailover=true` 并等待集群观察到，先排除晋升候选。
3. 只删除问题**备库**上 inactive、非 synced 的精确同名逻辑槽；绝不删除主库活动槽。
4. 等待 PG 原生重建。初期可能为 temporary，需源端正常解码推进，不能手工推进 LSN。本轮添加隔离探针事件及主库 CHECKPOINT 后转为永久同步槽。
5. 核对槽进度达到主库采样位点、角色未改变，恢复原 nofailover 值并确认生效。

工具 `repair-demoted-slot.py --node s3 --execute` 实施上述保护；只适用于已确认的这种故障。现在各槽健康，不要再运行修复。工具新增受保护的 `secrets/cdc/slot-repair-<node>.json`，在修改前保留原晋升标记；超时保留排除状态及记录。检查问题并等待原生同步后，可用同一命令加 `--resume` 只完成检查/恢复标记，**resume 不会再次删槽**。首次现场修复发生在加入该记录功能之前，采用人工核对后恢复标记，独立证据如实记录；新增断点继续分支仅通过 mock 安全测试，未人为制造第二次故障。

## 灾难恢复与尚未完成项

- publication 明确列出 `cdc_validation.outbox`，`publication.autocreate.mode=disabled`，不使用 FOR ALL TABLES。业务阶段应单独设计 `publication.outbox` 的角色、槽、topic 和快照交接；不能简单改现有 Connector 表名并沿用测试 offset。
- `snapshot.mode=initial` 只用于本次新建探针。槽失效、主库回退、Kafka offset 丢失或数据灾备后，先停止投递并保存 LSN/offset/事件 ID 证据，核对 outbox 保留和 Kafka 保留范围，制定重放/重快照方案，再恢复。不能丢弃旧 offset 或自动创建空槽掩盖缺口。
- PG 物理备份不等于 Connect offset/逻辑槽可直接协同恢复；Kafka 内部 topic 也没有独立灾备。本轮仅核对切主后归档及新差异备份成功，没有做 PG+Kafka 联合灾难恢复。
- Business DB 保持不变；未开发/验收 Publication 业务、Inbox/Apply 幂等消费者及外部数据库交付。分析 minimal outbox → ClickHouse 是独立链路，不共用这套消费位点。
- Kafka 仍 PLAINTEXT，PG 传输未启用 TLS，Connect REST 以内部网络/RBAC 为边界；SASL/TLS/ACL、持续告警、长时间断网/整机失联与容量验证仍待。

实现依据：PostgreSQL 17 官方 logical decoding / failover slot 文档、Debezium PostgreSQL Connector / Outbox Event Router 文档。验收证据见 `ops/checks/2026-09-21-cdc-reliability.md`。
