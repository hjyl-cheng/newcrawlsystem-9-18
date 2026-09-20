# 数据库 migration：首个事实表切片

`0001_collection_facts.sql` 创建 `crawler.channels`、`crawler.contents`。
`0002_comment_page_evidence.sql` 增加评论正文 Hash/profile/采集时间和页面检查水位；内部 Store 已实现指标与评论合并，见 [Store 说明](../docs/migration/content-store.md)。
`0003_metrics_submission.sql` 增加最小计划/执行授权/冻结目标及 Submission/回执/批次检查点，新增 control、ingestion Schema；详见 [受控提交说明](../docs/migration/metrics-submission.md)。
`0004_kafka_result_outcomes.sql` 增加 Kafka 记录处理结果及坏消息原文隔离；APPLIED 处理记录与事实/回执同事务提交，再确认 Kafka 进度。详见 [24.9](../24.9_Kafka采集结果入口与并发入库实施方案.md)。
迁移工具维护 `platform.schema_migrations`，每批仅创建有对应实现和验证的结构。
S1 的独立验证库已通过真实 PostgreSQL 17 测试，详见 `ops/checks/2026-09-20-database-facts.md`。
当前 `crawler` 正式目标库尚未应用本批 migration，Business DB 保持原样。

## 执行方式

迁移和集成测试使用 Node 24、`npm ci` 安装的 `pg`；不需要本机安装 psql。
凭据经环境变量提供，禁止写入版本库。以下假定已安全注入 `PGPASSWORD`：

```bash
export PGHOST=10.4.4.2 PGPORT=5432 PGUSER=crawler
export PGDATABASE=crawler_schema_test_20260920_facts01
export DB_EXPECTED_NAME="$PGDATABASE"
npm run db:migrate

# 只允许明确命名的独立测试库；不能把这个命令指向正式 crawler 库。
export DB_ALLOW_TEST_WRITES="$PGDATABASE"
npm run test:db
```

目标数据库必须事先由管理员创建并交给迁移账号，不由脚本隐式创建/删除。
迁移必须直连 PostgreSQL 主库，不能经过 PgBouncer transaction pool：会话级 advisory lock 需要一条独占连接。
工具检查实际数据库名、主库身份和 PG 17+；超时设置为锁 10 秒、单语句 60 秒。
同一迁移序号只能有一个文件；已应用历史必须是本地文件的完整前缀，并匹配文件名和 SHA-256。
每个 SQL 文件和对应版本记录在同一事务提交；重复执行跳过已完成文件。
不提供自动 DROP/降级，修改已发布脚本会被拒绝。断线后重连重跑，依据 ledger 决定是否已提交。

## 事实字段与本轮边界

- 频道保留采集事实、质量/来源、日期精度和生命周期证据。旧调度/Agent 执行字段留给后续 control 表。
- 内容用不透明 `content_key`，默认随机 UUID 文本；`platform/resource_kind/source_content_id` 唯一。
  video/short/live 共用 youtube_video 命名空间；post 使用 youtube_post。
  普通 UPDATE 不允许改主键、来源身份或所属频道；类型纠正允许 video ↔ short ↔ live。
- 评论第一页是同表 JSONB，可为 SQL NULL 或合法空页；禁止 JSON null、缺键、错误类型、非法采集日期及数量/数组长度不符。
  collected_at 是原正文的采集时间；当前 comment_count_observed_at 单独记录。
  不强求历史页 total_count 等于当前 comment_count，不设一个未经确认的全局 20 条上限。
  实测默认压缩为 pglz，EXTENDED 允许自动压缩及行外存储；大模拟页已验证压缩生效，小值不一定压缩。
  JSON 元素当前仅验证为对象，评论项具体字段/total_count 正文协议仍待采集切片定义。
- 未知指标用 NULL + unresolved/unavailable；有效数字必须配对相应状态，零值证据必须为 0。
  频道认证、日期、业务邮箱和评论禁用增加防 NULL 漏洞约束。
  bigint 用于采集指标，Node pg 以字符串返回，禁止无条件转 JS Number；采集事实指标与 0003 的 20 位执行/输入版本分别使用 bigint 和受 CHECK 约束的 numeric 域。
- `is_members_only` 改为可空，未知不写成 false；public/unlisted 与 false、members_only 与 true 配对。
  外部兼容适配尚未实现，不能宣称旧消费者可直接读取新库。记录在 24.7 DB-12。
- 初始索引只含身份/主键及 channel+published_at、channel+type+last_seen_at；后续查询形成后再测执行计划加索引。
- `last_source_plan_id`、`last_observation_id` 等依赖表尚未创建，本批不建悬空引用；后续连同 Plan/Observation 加外键。
  不把旧 run_id 直接当新 plan_id，也不让裸 JSON 代替授权/回执。
- about/player/next 旧 Hash 暂为 text；publication_item_hash 明确保留 sha256: 前缀。
  0002 已定义正文独立 Hash/profile，仅作本地完整性校验，不能借用旧 Hash 或数据库 Hash 当 JCS Hash。

内部内容 Store 已实现三种数量、非空正文保留/空页补齐、逐字段观察顺序、并发串行合并和事务回滚。
内部最小授权复核与 Submission/APPLIED/批次检查点原子事务已实现；Kafka 签名与消费适配已在验证环境测试，生产凭据/权限、Outbox、完整字段 Store 和线上服务写入仍未完成。
频道 `updated_at` 仍待频道 Store；当前内容 Store 设置所覆盖指标的观察时间及 first/last_seen_at，不冒充完成 player/next 整组观察。
本批账号是验证迁移 owner；运行时最小权限账号、Worker 禁止直写由后续服务部署落实，不能把内部 Principal 参数视为已完成真实身份认证。

Kafka 适配从受信公钥登记中取得 holder/频道权限，再复核库内执行权；本次只用内存测试密钥，没有部署真实 Worker 身份与密钥管理。

## Kafka 结果链路验证

沿用上述独立测试库与 PG 环境变量，再设置：

```bash
export KAFKA_BROKERS=10.4.4.2:9092,10.4.4.8:9092,10.4.4.5:9092
npm run kafka:provision
npm run test:kafka
```

provision 只创建/检查专用验证 Results Topic；已有 Topic 的参数须另用官方 kafka-configs.sh 复核，不自动覆盖。
测试会应用 migration、创建唯一临时 Topic/消费者组及业务样例，结束时按本轮身份清理；不要指向正式库或生产 Topic。
保留验证库空表、迁移记录和永久验证 Topic。结果见 `ops/checks/2026-09-20-kafka-results.md`。

## 验证与恢复

`npm test` 是无数据库的契约/应用测试；`npm run test:db` 是显式数据库集成测试。
测试库名必须以 `crawler_schema_test_` 开头，并同时匹配两个确认环境变量。
单连接约束测试在事务中构造样例并回滚；Store 并发测试会提交带随机前缀的样例，随后按本轮创建的频道精确清理。
空库运行还会先验证带评论样例的 0001 → 0002 升级；已升级库重跑时该项跳过。测试库保留表和版本记录以便检查，不包含业务数据。
GitHub 的独立数据库 workflow 使用临时 PostgreSQL 服务，不连接六台服务器；本轮仅本地/S1 已执行，远程 workflow 待提交后运行。

migration 失败时该文件的 DDL 和 ledger 一起回滚；已经成功的更早文件不会自动撤回。
成功建表后如发现问题，应添加后续 migration 或回退兼容应用，不能改 ledger 或删除有数据的表冒充回滚。
生产应用前须完成 Store/权限/回执验收与备份恢复准备；本次不提供备份和 HA 达标结论。
