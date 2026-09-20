# 内容指标与评论 Store 验证

日期：2026-09-20。代码唯一工作目录：newcrawlSystem。

## 本轮结果

- 新增内部 TypeScript Store：`services/facts-store/src/content-store.ts`。
- 新增迁移：`0002_comment_page_evidence.sql`；未修改 0001 已应用文件。
- S1 PostgreSQL 17.11 实库集成测试：24 项通过、0 失败、0 跳过。
- 既有无数据库测试：19 项通过、0 失败；TypeScript 编译通过。
- database workflow 新增 Store/tsconfig 路径触发，集成命令先编译；YAML 语法检查通过。远程 GitHub workflow 本轮未运行。
- 旧参考仓库工作树仍干净；未向旧仓库或外部 Business 写入。

## 数据库与迁移证据

原隔离库 `crawler_schema_test_20260920_facts01` 已升级到 0002，完成首轮 23 项回归/Store 测试。
随后专门创建 `crawler_schema_test_20260920_store02` 验证有数据升级：只应用 0001、插入合法评论样例、再执行 0002，证明正文保留且证据正确补齐。
该库执行新增升级用例后的完整 24 项测试均通过。确认两个验证库的 channels/contents 均为 0 行后，已删除此次临时 store02 库。
原 facts01 保留空表与 0001/0002 台账供复核；正式目标 crawler 尚未应用这两批迁移。

| migration | SHA-256 |
|---|---|
| 0001_collection_facts.sql | 0e5919d99f13509eab6eaac941c26b3b530fae5339f74419bd02f47932b1e40b |
| 0002_comment_page_evidence.sql | f47abb1779e17f21fc8af2cc8ef219faf955c9e2e3b9e127d906c87f608c55e6 |

facts01 的 0002 applied_at：2026-09-20T02:58:06.465Z。
临时升级库的 0001/0002 applied_at：2026-09-20T02:59:30.295Z / 02:59:30.309Z。
新增四列后，contents 为 63 列；频道仍为 61 列。

## 本轮新增的实际行为验证

1. 大于 JS 安全整数范围的合法 bigint 精确往返，评论正文及本地 Hash/采集时间正确生成。
2. 数量 100→120 时仍保留原非空评论页、原 Hash、原采集时间；页面检查水位独立推进。
3. 缺失/未知数量不覆盖旧数量及来源/时间，失败观察不阻挡更早但仍较当前更新的有效观察。
4. 明确禁用写 0，不删除历史正文；后续有效非禁用观察可更新状态。
5. 缺失/空页补齐；较旧页面不能越过较新的有效页面检查；空页→空页保留原正文时间。
6. 各指标观察水位独立；真实数量下降允许，不使用 MAX(count)。
7. 等时同值重复稳定；等时异值冲突回滚先前字段和整批其他内容，调用者吞掉错误也不能提交。
8. 错频道、未实施的类型纠正、非法输入和未来页面时间明确拒绝。
9. 后续 SQL 失败使事实回滚；被捕获的 SQL 错误也不能被当作提交成功。
10. 两个独立池连接并发首次写入同一视频，只产生一个身份，数量保留最新有效观察；首先接受的非空页保留。
11. 正文证据不能单独篡改成另一套 Hash/时间；关闭后的事务对象不能复用。
12. 原 0001 数据升级 0002 后正文不变，Hash/采集时间/页面检查时间从既有页正确补齐。

## 限制

目前是可信内部 Store 切片，不是带执行授权的完整写入入口。
Plan/epoch/目标检查、Submission 身份与幂等回执、COMMIT_UNKNOWN 对账、Outbox、运行时最小权限、真实网络采集与故障恢复未实施。
频道 Store、完整内容字段策略、帖子、类型纠正也未在本轮实现。
Hash 采用显式 PG17 JSONB 文本 profile，不能与 JCS payload_hash 或 Publication Hash 互换。
评论仍为同表 JSONB + EXTENDED/TOAST 默认 pglz，本轮没有改变压缩策略，也没有新增生产业务库。

合并规则和接口使用见 `docs/migration/content-store.md`；架构差异已同步 24.7 DB-13 及实施章节。
