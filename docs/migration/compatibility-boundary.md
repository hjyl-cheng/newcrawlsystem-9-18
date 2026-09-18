# 兼容边界与重新实现规则

## 需要保留的业务行为

以下内容从 `oldjiagousys` 提取证据并通过回放/固定样本验证，但在新仓库重新实现：

- Discover、Full、Incremental、Data API、Agent 的业务判定；
- 三 Clock 的推进、暂停、恢复和窗口语义；
- Channel/Video/Submission/Frame/Connection 的稳定身份；
- HTTP/响应/字段证据、Disposition、原因码和恢复条件；
- 中途提交、checkpoint、APPLIED/RECEIVED、幂等和 `COMMIT_UNKNOWN` 处理；
- Publication revision、sequence/hash、Inbox/Apply 和交付回执；
- Rota 身份、租约和执行授权语义；
- 已确认的 PostgreSQL 约束、触发器保护和领域不变量。

## 必须重新实现的边界

- Temporal Workflow 和 Activity 适配；
- Planner/Starter/Query Engine 的 Plan 授权和 Query 自生长闭环；
- Worker → Data Ingestor → PG 的结果提交路径；
- Read Model、Execution Trace Projection 和数据水位；
- Business Console 的 Current/History/Operational 读取面；
- `publication.outbox → Debezium → Kafka → Business Consumer`；
- SeaweedFS 大对象引用和恢复路径；
- Kubernetes、Argo CD、资源限制、滚动发布和 Worker 扩容。

## 代码差异验收

任何从旧系统借鉴的实现都必须有：

```text
旧行为样本
→ 新契约输入
→ 新实现输出
→ 领域事实/原因码/回执对照
→ 差异说明
→ 测试或 ADR
```

没有对照证据的代码不得因为“旧系统已经这样做”直接进入新架构。复制文件、复制类名、复制队列配置或只替换服务名称均不构成迁移完成。
