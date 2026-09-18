# 运行时与部署边界盘点

盘点对象：旧 `deploy/compose*.yml`、`runtime/`、`services/*/Dockerfile`。  
目标：把旧运行时依赖转化为 24.4 的部署输入，不把 Compose 拓扑原样搬入 Kubernetes。

## 旧运行时观察结果

```text
Nginx/Auth/Dashboard
        │
QYBullMQ API + Bull Board + 多类 BullMQ Worker
        │
Redis / Crawler PostgreSQL / Rota PostgreSQL / Business PostgreSQL
        │
NATS、MinIO、远端节点和本地 Agent
```

旧系统使用 Compose 文件按模式组合服务，`services/qybullmq` 同时承担 API、Controller、Worker、Finalize、Publication 和大量维护脚本。

## 24.4 目标运行时

```text
A1/A2/A3 Kubernetes
  Argo CD
  Temporal / Planner / Query / Starter
  Discover / Full / Incremental / Data API / Agent Worker
  Data Ingestor / Feature / Projector / Console API

S1/S2/S3 受控有状态部署
  PostgreSQL Primary/Standby + PgBouncer
  Kafka 3 Broker（仅 Distribution Plane）
  SeaweedFS 3 节点
  ClickHouse（验证期单节点）
```

## 依赖迁移判断

| 旧依赖 | 24.4 处理 | 说明 |
|---|---|---|
| BullMQ/Redis 内部任务队列 | Temporal + Kubernetes Worker | 先验证业务身份和重试语义，再切换调度实现 |
| NATS whole-channel 结果通道 | Data Ingestor | 不建立生产双通道；旧协议只做回放对照 |
| MinIO/对象存储 | SeaweedFS S3 | 只保存大型 Raw、恢复对象和超大 Submission |
| Compose | Kubernetes + Argo CD | 只提取依赖、健康检查和资源需求 |
| 旧业务数据库直连 | Business Consumer 独立落库 | Crawler 只发布 versioned Publication Event |
| 旧 Dashboard/Bull Board | Current/History/Operational 三类读取面 | Console 读投影，控制写回权威 Store |

## 发布链

```text
newcrawlSystem commit
  → CI 测试/构建镜像
  → Registry immutable digest
  → Argo CD validation overlay
  → Kubernetes A 节点
  → S 节点由 Pigsty/系统服务/受控部署管理
```

Kubernetes 不负责替代 PostgreSQL、Kafka、SeaweedFS 的数据运维责任；Argo CD 也不负责业务授权、Plan 结算或数据库事实判断。
