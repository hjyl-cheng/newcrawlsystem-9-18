# 数据库与事实边界盘点

盘点对象：`oldjiagousys/database/bootstrap/crawler.sql`、`database/bootstrap/business.sql`、`database/reference-snapshots/feature.sql`。  
状态：只读盘点；新系统必须使用显式 migration 重新建立，不直接复制旧 dump 作为最终 DDL。

## 旧系统观察结果

旧 Crawler SQL 至少包含三个主要 schema：

```text
crawler
feature_clock
publication
```

观察到的旧事实对象包括：

- `crawler.channels`、`channel_runs`、`channel_candidates`、`content_candidates`、`contents`；
- `crawler.query_terms`、`query_sets`、`query_pages`、`query_dispatch_batches`、`query_quality_*`；
- `crawler.incremental_youtubejs_video_batches/items`、`crawl_observations`、`task_events`；
- `crawler.migration_*`、`youtube_api_*`、`agent_*`、`raw_objects`；
- `feature_clock.channel_clock_state`、`daily_channel_plans`、`dispatch_outbox`、`clock_decision_log`；
- `publication.revision`、`stream`、`outbox`、`channel_delivery_state`、`domain_current`。

旧业务库包含 Creator Search、Publication Projection、Inbox/Apply 等业务投影和迁移状态。

## 24.4 目标事实域

仍然是一套逻辑 Crawler PostgreSQL，允许 Primary/Standby，但不做应用级分片：

```text
Crawler PostgreSQL
├── CrawlerStore：Channel、Candidate、Content、Observation、Execution
├── PlanStore：QueryRun、Plan、Clock、租约、结算
├── FeatureStore：Feature、Reference、Policy、Recalculation
├── Read Model：Current Read Model、Execution Trace Projection
├── publication.outbox：唯一跨系统业务 Publication Outbox
└── analytics/minimal outbox：独立 ClickHouse 分析投递
```

业务控制台读模型可删除和重建，不能成为授权源、写入判定源、Clock 推进源或 Publication 幂等源。

## 必须重新设计的数据库工作

1. 先建立目标对象和版本/身份契约，再决定旧表的迁移映射。
2. 将高频写入、权威事实、投影和分析事件分别标明事务责任。
3. 明确 T1～T4 的事务范围、`RECEIVED/APPLIED` 回执和重复提交行为。
4. 只显式创建包含 `publication.outbox` 的 PostgreSQL Publication。
5. 为 Read Model 和 Execution Trace 增加 `source_watermark`、`updated_at` 和重建游标。
6. 对旧表只保留确有业务证据的字段；不因为旧表存在就全部复制到新模型。
7. 每次 schema 变更必须有 migration、验证 SQL、回滚/恢复说明和固定样本测试。

## 明确禁止

- 直接把旧 `crawler.sql` 当成 24.4 最终 schema；
- 把旧 BullMQ/Redis 状态表当成 Temporal 的权威状态；
- 让 Read Model 或 ClickHouse 反向授权业务写入；
- 将普通 Delta、遥测或内部任务结果写入 `publication.outbox`；
- 用跨库双写替代单逻辑 PG 事务。
