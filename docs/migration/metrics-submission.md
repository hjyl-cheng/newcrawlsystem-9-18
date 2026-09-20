# 最小受控提交：计划、执行权、事实、检查点和回执

日期：2026-09-20。实现：`services/ingestion/src/metrics-submission.ts`；结构：`0003_metrics_submission.sql`。
已在 S1 隔离库完成真实 PostgreSQL 事务、并发与提交确认丢失模拟验证，未部署网络服务。

后续 Kafka 接入已实现：`services/kafka-results` 验证 Ed25519 签名，由可信公钥登记构造 Principal，再调用 `prepareMetricsApply`。
该函数在事务外校验并固定输入，返回在调用方事务中执行的应用函数；`applyMetricsSubmission` 仍提供原有独立事务入口。
Kafka 消费者将事实、Submission、检查点、APPLIED 与 `ingestion.kafka_record_outcomes` 放在同一 PG 事务内，提交成功后才提交消费进度。
当前规则以根目录 24.9 为准，下面的内部同步 Apply 描述不代表 Worker 继续直连 gRPC。

## 本轮实现的链路

```text
可信控制代码 prepareMetricsPlan
  └─ 计划 + 活动频道协调行 + 执行授权 + 冻结输入/目标 + 逻辑批次

可信接入代码提供认证后的 Principal
  └─ applyMetricsSubmission（一个数据库事务）
      ├─ 复核正文格式、JCS 内容 Hash、条数/大小上限
      ├─ 锁 submission_id；已经成功 → 返回原回执
      ├─ 锁频道协调行、计划及执行授权
      ├─ 核对 holder、scope、epoch、租约、活动计划
      ├─ 核对冻结输入、完整目标集合；同批其他提交已成功 → 拒绝
      ├─ 保存不可变 Submission 身份与规范正文
      ├─ 调用内容 Store，合并每个视频的指标/评论
      ├─ 写 APPLIED 回执 + 批次检查点（每项合并结果）
      └─ COMMIT 成功后才返回回执

COMMIT 确认丢失 → DB.COMMIT_UNKNOWN
  └─ querySubmissionReceipt（原身份 + 原内容 Hash）
      ├─ APPLIED：复用原 receipt_id、recorded_at
      └─ NOT_OBSERVED：当前查询没看到；不是永久“未提交”结论
```

`prepareMetricsPlan` 是内部控制原语，绝不能把它暴露成 Worker 自行授予写入权的接口。
相同计划/意图/参数重复准备不会重新续租或复活执行权；不同参数使用同一身份则拒绝。
接管使用 `takeOverExecution`，锁频道和授权后比对原 epoch，再增加 1；普通提交重试不增加 epoch。
当前没有完整取消/续租/控制命令幂等 API，也没有启动 Temporal Workflow。

## 明确限定的切片

- 只接受 incremental + delta，正文 Schema 为 `content.metrics.batch/1-draft.1`。
- 已有频道、一个活动计划、一份冻结 VIDEO_METRICS 义务、一个有界逻辑批次，1～100 个视频，规范正文不超过 1 MiB。
- VIDEO_METRICS 是本切片的内部义务标识；不是已定义完成的完整 VIDEO 领域证明。后续真实 Planner 接入时需迁移到最终领域/阶段模型。
- plan.kind 为 INCREMENTAL，但当前计划行仅提供绑定与活动/取消状态，未实现全量规划字段、预算、期限、覆盖、Feature 依赖或结算。
- 目标集合包含 source_content_id/content_type，按 source_content_id 稳定排序计算 target_hash。
  提交必须完整匹配冻结集合，重复、遗漏、额外目标均不能形成成功回执；不支持当前切片的部分批次成功。
- 1 个提交中的未知或较旧指标可以由 Store 保留旧值，检查点保存其 unknown/older 等结果。
  APPLIED 是“该提交按规则应用完成”，不是“所有字段都是新鲜有效值”。
- 没有开启 RECEIVED、对象模式、分帧、gzip、后台 Apply 或恢复队列。本轮是同步 Fast Apply。

这些限制不是 24.4 全局功能范围，更不能用本批的“一义务一批次”唯一约束套用未来全部采集角色。

## 新表及事务边界

| 表 | 本轮职责 |
|---|---|
| control.plans | 计划与频道绑定，意图去重、活动状态 |
| control.channel_coordination | 同频道短事务协调锁及当前活动计划 |
| control.execution_authorizations | scope/holder/epoch/租约写权 |
| control.domain_obligations | 本切片的冻结代次、输入/目标 Hash、策略和已封口目标 |
| control.domain_items | 本批冻结目标及稳定顺序，不是完整 DomainItem 结算状态机 |
| ingestion.logical_batches | 不含执行 epoch 的稳定工作身份 |
| ingestion.submissions | 原始执行上下文、正文 Hash 和完整规范字节，不可 UPDATE |
| ingestion.receipts | 不可 UPDATE 的唯一 APPLIED；与提交/批次/计划/频道/Hash 复合绑定 |
| ingestion.batch_checkpoints | 与该回执绑定的条目数量和每项 Store 合并结果 |

0003 保留全部规范正文 bytea，便于本切片审计核对，尚未定义生产清理策略。
这不是原始 wire 字节，也不能用 JSONB 重算 wire Hash。本轮尚未接收原始网络 Manifest。
错误/冲突直接拒绝且回滚；拒绝的提交没有 RECEIVED 耐久责任，不伪造“另一个提交成功”的回执。

应用的锁顺序是 submission advisory 锁 → 频道协调行 → 授权/计划 → 批次 → 按来源 ID 排序的内容行。
接管复用频道协调锁，因而无法在已通过授权检查的短事务中间切走执行权。
租约在持锁授权检查时用数据库 clock_timestamp 判断；已获准事务与后续接管按锁顺序串行。
不能将此边界解释为强制中断已经在外部网络运行的旧 Worker。
锁等待上限 10 秒、单 SQL 30 秒；没有完成跨整个批次的生产预算/背压验收。

## 身份、Hash 与权限

- 相同 submission_id + 相同业务身份/内容 Hash：返回原回执，不重复合并事实。
- 相同 submission_id + 不同身份或内容：IDEMPOTENCY.PAYLOAD_CONFLICT。
- 不同 submission_id 争同一 logical batch：IDEMPOTENCY.BATCH_ALREADY_APPLIED，不能返回别人的成功。
- 成功回执查询不要求当前执行 epoch，接管/取消后仍可对账；必须具备该频道读权限并提供匹配身份和 Hash。
- Principal 的频道权限与 holder 必须来自可信适配层，不得信任 Worker 在正文中自报。Kafka 适配已用公钥登记构造该身份；内部函数参数仍是可信接口，生产密钥/权限管理尚未部署。
- epoch/generation/input_revision 使用受 CHECK 约束的 numeric 域，接受 0～99999999999999999999 的整数。
  不用 numeric(20,0) 静默四舍五入小数；JS 始终传十进制字符串。接管溢出失败。

正文 Hash 使用 JCS 的受限 JSON 子集：按 UTF-16 key 排序、保留数组顺序、标量 Unicode、安全整数数字。
大计数/版本为字符串；不接受浮点数、不安全整数、孤立 surrogate、undefined 或隐式 toJSON 对象。
编码器已与此前协议黄金字节/Hash 对照，但不是任意浮点 JSON 的通用 JCS 库。
与评论列的 pg17-jsonb-text-sha256-v1 完整性 Hash 分开，禁止互用。

正文 Schema：`contracts/data-plane/content-metrics-batch.v1-draft.schema.json`。
固定样例：`contracts/data-plane/fixtures/content-metrics-batch.json`。
运行时另检查 bigint 上界、页面数组长度、页面时间、冻结目标、Hash 和数据库授权；Schema 通过不等于允许落库。

内部 request 的 submission_id/execution/content_sha256 对应 24.6 的 identity.submission_id/execution/payload.content_sha256。
24.6 的 Manifest/Frame 是旧传输草案，当前 Kafka 使用独立签名记录版本，校验频道 key、签名、规范字节、业务形状与内容 Hash。
没有实现旧 gRPC Submit、分帧、OBJECT 或压缩协议；未来如增加这些模式，应另定义并验证对应限制。
本轮输出 APPLIED / NOT_OBSERVED 对象已通过 24.6 JSON Schema；新增内部错误码尚未扩充进对外 problem Schema，需适配后才能上线。

## 提交确认丢失与验收结果

事务封装对 COMMIT 确认异常保守返回 DB.COMMIT_UNKNOWN；明确的序列化失败/死锁仍保留其错误码。
丢弃受影响连接，按原提交身份和 Hash 查询；NOT_OBSERVED 是该查询语句快照的观察，可以在原事务随后提交后变成 APPLIED。
不会自动改提交 ID、宣称没有提交，或重建一份假的否定回执。

真实 PG 测试用客户端拦截器分别模拟“执行 COMMIT 后丢确认”和“COMMIT 前断开”，核对原回执/重试结果。
这验证应用恢复逻辑，不是网络故障、PG 主备切换或 RPO 验收。
最终 38 项数据库测试、21 项无数据库测试通过；记录见 `ops/checks/2026-09-20-metrics-submission.md`。

后续 Kafka 更新：9 项真实 Kafka/PG 测试通过；重跑数据库测试为 37 项通过、1 项已升级库跳过；24 项无数据库测试通过，见 `ops/checks/2026-09-20-kafka-results.md`。
Kafka 路径额外先获取 record advisory 锁，再进入上述 submission/频道锁顺序。坏消息回滚后另存 QUARANTINED 原文，不冒充 APPLIED；数据库或提交不确定错误不推进 Kafka 进度。
下一步：打包常驻 Data Ingestor 消费服务，落实凭据/ACL、最小 PG 权限、健康/积压指标及回执/隔离查询，再部署验证环境。
正式目标 crawler 未应用这些迁移，业务库未改；Temporal、真实 Worker、Domain/Plan 结算和 Publication 后续分别推进。
