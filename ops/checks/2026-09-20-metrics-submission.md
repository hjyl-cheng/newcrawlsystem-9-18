# 最小计划、执行授权与 Submission 原子事务验收

日期：2026-09-20。目标：S1 PostgreSQL 17.11 独立验证库。

## 变更与结果

- 新增 `0003_metrics_submission.sql`：5 张 control 表、4 张 ingestion 表，以及 ID/Hash/20 位整数域。
- 新增 `services/ingestion/src/metrics-submission.ts`：可信计划准备、显式 epoch 接管、同步 Apply、原身份回执查询。
- 内容 Store 共用事务接口增加 COMMIT 确认未知错误；确认丢失不当成“确定没提交”。
- 新增视频指标正文 Schema/固定样例；APPLIED/NOT_OBSERVED 输出与既有 24.6 envelope Schema 相容。
- 最终 `npm run test:db`：38 项通过，0 失败，0 跳过。
- 最终 `npm test`：21 项通过，0 失败；TypeScript 编译通过。
- Git diff 空白检查、数据库工作流 YAML 检查通过；GitHub workflow 尚未远程执行，未部署业务服务。

## 数据库记录

原验证库 `crawler_schema_test_20260920_facts01` 已应用 0003，applied_at 为 `2026-09-20T03:13:26.998Z`。
0003 SHA-256：`6524d487f86ab5f65adb49de4d8b876c050a63125b0aba95b5358552cce88ae9`。
0001/0002 文件及台账校验和保持原值。

为完整验证空库与有数据升级，新建 `crawler_schema_test_20260920_submission03`：
测试先用 0001 插入评论样例，再升级 0002/0003，随后运行全部集成测试。
临时库的 0003 applied_at 为 `2026-09-20T03:16:41.921Z`。

验收后逐表检查两个验证库：channels、contents、plans、channel_coordination、execution_authorizations、domain_obligations、domain_items、logical_batches、submissions、receipts、batch_checkpoints 均为 0 行。
本轮临时 submission03 库已删除；原 facts01 保留空结构及 3 条迁移台账。
正式目标 crawler、外部 Business 与旧参考仓库未执行本批变更。

## 关键验收

1. 首次 Apply 同时产生事实、不可变规范正文、批次检查点和 APPLIED；计划仍保持 active，未冒充结算成功。
2. 同提交重复/并发返回完全相同 receipt_id/time；同 ID 不同内容拒绝。
3. 两个 Submission 争同一 batch，仅一个成功；失败者没有借用成功者回执。
4. 同 submission_id 跨频道并发争用也只允许一种身份/内容，另一方冲突且不写事实。
5. 错 holder、错 plan/channel、错冻结输入/目标、过期/撤销授权和取消计划均不能产生新效果。
6. epoch 从 9007199254740993 正确增加；旧 epoch 被隔离，原成功结果在接管/取消后仍能对账。
7. 20 位 generation/input_revision/epoch 精确保存；小数、负数、溢出拒绝；普通计划准备重试不续租。
8. 冻结批次缺项或重复目标不能通过；正文变化但 Hash 没变化时拒绝。
9. 写完事实和回执后人为令检查点 SQL 失败，所有事实/Submission/回执/检查点一起回滚；同身份随后可重试成功。
10. COMMIT 后模拟客户端丢失确认：返回 DB.COMMIT_UNKNOWN，原身份查询得到已存在 APPLIED；重试复用原回执。
11. COMMIT 前模拟客户端失败：回滚后查询得到 NOT_OBSERVED，原身份重试成功；没有永久否定回执。
12. 在 Apply 尚未提交时查询可得到 NOT_OBSERVED，事务完成后同查询得到 APPLIED；接管与正在提交的写入按频道锁串行。
13. 新正文编码与既有 JCS 黄金字节/Hash 一致；超出该受限 JSON 子集的危险/有损输入拒绝。
14. 上两轮的身份、评论保留/补齐、观察顺序、迁移校验和、回滚和升级用例全部回归。

COMMIT 故障通过客户端代理在真实 PG 提交前后注入，不是机器宕机/主备切换或真实网络中断演练。

## 限制与下一步

仅实现 incremental/delta 视频指标、一个活动计划/一份局部义务/一个有界批次；未冻结全部架构字段或对外服务协议。
没有 RECEIVED、对象接收、后台恢复、DomainReceipt、完整计划结算、预算/Feature、Outbox/业务交付或真实身份认证。
VIDEO_METRICS 与按 batch/source 的 domain_items 是本切片结构，后续真实 Planner 需要明确迁移/扩展，不能当成最终多角色模型。
当前数据库 owner 仍有直接 SQL 权限；最小权限和认证接入须在网络服务部署前完成。
下一步为已验证切片补齐认证、Manifest/传输校验、错误映射与最小 Data Ingestor 服务接口，再部署验证环境。
具体接口与边界见 `docs/migration/metrics-submission.md`；差异同步 24.7 DB-01、DB-14 及第 19 节。
