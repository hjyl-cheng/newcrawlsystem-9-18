# 旧系统到 24.4 的模块映射（初始盘点）

盘点对象：本地 `oldjiagousys`，参考提交 `bee141e41b609a583a2b86b34967fce1af404a80`。  
目标代码来源：`newcrawlSystem`。  
状态：只读盘点，不代表已经完成迁移。

## 使用规则

旧模块只用于确认行为、原因码、数据库语义和测试样本。新实现必须在 `newcrawlSystem` 中按 24.4 边界重新设计；本表不是复制清单。

| 旧位置 | 旧职责（观察结果） | 24.4 目标边界 | 处理方式 |
|---|---|---|---|
| `services/qybullmq/src` | BullMQ API、队列 Worker、Controller、采集、Finalize、Publication、恢复脚本集中在一个代码树 | Control Plane、Execution Plane、Data Plane、Distribution Plane 分开 | 重新划分接口，按契约重实现 |
| `services/qybullmq/src/worker.js` | 统一队列 Worker，根据队列和身份执行不同任务 | Discover、Full、Incremental、Data API、Agent 五执行角色 | 保留行为证据，重新接入 Temporal Activity 和 Data Ingestor |
| `services/qybullmq/src/controller.js` | 旧控制器和队列接单逻辑 | Planner/Starter、Plan 授权和 Temporal Workflow | 适配语义，禁止把 BullMQ 状态直接当新事实 |
| `services/qybullmq/src/*Publisher*` | 旧 Publisher/Publication 发送逻辑 | `publication.outbox → Debezium → Kafka → Business Consumer` | 重新实现边界，保留 event identity/幂等语义 |
| `services/qybullmq/src/remoteNodes` | 远端节点连接、执行交接、结果回流 | Worker → Data Ingestor；大对象才走 SeaweedFS | 按 Submission/Frame/Connection 契约重实现 |
| `services/feature-engine` | Feature 状态和增量时钟相关逻辑 | Feature Store、三 Clock、Plan Settlement | 保留规则，拆出 Store 和事务边界 |
| `services/feature-dispatch` | Dispatch Outbox/队列投递 | Planner/Starter 或 Temporal Activity | 不把旧队列直接搬入新系统 |
| `services/local-agent` | 本地 Agent、模型包、Profile 推断 | Agent Batch 执行角色 | 保留模型/业务行为，重新实现任务和结果契约 |
| `services/remote-node` | 远端节点运行时和采集镜像 | 可扩展 Worker 节点 | 只提取协议和恢复样本，不复制镜像拓扑 |
| `services/rota` | Rota、代理槽位、身份策略 | Rota/网络身份适配边界 | 保留执行授权和身份语义，独立于调度状态机 |
| `services/dashboard` | 旧操作面板、Bull Board、节点管理 | Business Console、Current Read Model、Execution Trace | 重新实现读取面和控制写复核 |
| `services/auth` | 旧认证网关 | Console Auth/RBAC | 重新实现权限边界，控制命令回权威 Store |
| `deploy/compose*.yml` | Docker Compose 运行拓扑 | Kubernetes + Argo CD；S 节点受控部署 | 只提取依赖和环境变量，不能直接转成生产清单 |
| `deploy/nats`、BullMQ/Redis 配置 | 旧内部传输/队列 | Temporal、Data Plane、PG Outbox、Kafka | 作为兼容参考，首期不新建旧双通道 |

## 新代码第一批目录

最终目录以实现为准，首期建议建立以下边界：

```text
apps/
  control-plane/       # Planner、Starter、Query、Temporal adapters
  data-ingestor/       # Submission 接收、批量 Apply、回执
  execution-workers/   # 五角色共用执行内核和角色适配
  console-api/         # Current/History/Operational 读取面和控制命令
  projectors/          # Read Model、Execution Trace
  distribution/        # Outbox schema、Debezium/Kafka 配置、Consumer 契约
packages/
  domain-contracts/
  crawler-store/
  plan-store/
  feature-store/
  execution-kernel/
  observability/
database/
deploy/
tests/
```

这些目录用于表达新边界，不要求把旧目录原样搬过来。
