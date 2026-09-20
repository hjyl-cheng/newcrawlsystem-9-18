# Kafka 采集结果入口验收记录

日期：2026-09-20。对应用户确认的 Kafka 结果入口调整，方案见根目录 24.9。
结论：最小增量视频指标切片已在真实三 Broker Kafka 和隔离 PG 跑通；本次没有部署常驻消费者或真实采集 Worker。

## 环境与实际配置

- 执行位置：A1，唯一新代码仓库 `/home/ubuntu/workspace/newcrawlSystem`。
- Kafka：S1 `10.4.4.2:9092`、S2 `10.4.4.8:9092`、S3 `10.4.4.5:9092`，Kafka 3.9.1，内网 PLAINTEXT 验证环境。
- PostgreSQL：S1 PG 17.11，独立库 `crawler_schema_test_20260920_facts01`；本批没有向正式 crawler 或外部 Business 执行迁移/写入。
- Node 24.21.0；固定客户端 `@confluentinc/kafka-javascript@1.10.1`，librdkafka 2.15.1。干净 `npm ci` 和编译通过，安装时 audit 为 0 个漏洞。
- 签名测试使用内存生成的 Ed25519 密钥；没有把私钥或数据库密码写入仓库。

永久验证 Topic 为 `crawler.results.validation.v1`，3 分区、每分区 3 副本。验收时分区 0/1/2 leader 分别为 Broker 3/1/2，三个分区 ISR 均包含全部三个 Broker。
独立 Publication Topic `crawler.publication.v1` 保留，本轮不生产或消费该 Topic。

已通过 S1 官方命令核查实际配置：

```bash
/opt/kafka/bin/kafka-configs.sh --bootstrap-server 10.4.4.2:9092 \
  --entity-type topics --entity-name crawler.results.validation.v1 --describe
```

| 配置 | 实际值 |
|---|---|
| min.insync.replicas | 2 |
| cleanup.policy | delete |
| retention.ms | 604800000 |
| retention.bytes | 134217728（每分区） |
| segment.bytes / segment.ms | 16777216 / 3600000 |
| max.message.bytes | 1100000 |
| unclean.leader.election.enable | false |

与 `infra/kafka/validation-results.json` 相符。保留窗口同时受容量限制，不保证消息一定保存满 7 天。

## 测试结果

| 命令 | 结果 |
|---|---|
| npm test | 24 通过，0 失败，0 跳过 |
| npm run test:db | 37 通过，0 失败，1 跳过 |
| npm run test:kafka | 9 通过，0 失败，0 跳过 |

数据库跳过项是带已有评论数据的 0001 → 0002 升级：本次复用已迁移库，因此不会重复执行升级场景；此前独立升级验收见 Store 记录。
Kafka 测试文件为 `test/kafka/results.test.mjs`，覆盖：

1. BROKER_ACKED 与 PG 回执分开，处理持久化后才推进 offset。
2. 相同 Submission 多条消息只生成一个业务回执。
3. PG 失败时不提交当前或后续 offset，重试保持分区顺序。
4. PG 已提交、Kafka 进度未提交时重启相同消费组，安全重放。
5. 签名错误消息保存原文隔离，后续合法记录继续处理。
6. 排队期间发生执行权接管，旧 epoch 隔离，不生成假 APPLIED。
7. 两个真实消费者属于同组，使用同步屏障证明在不同分区同时处理。
8. 消息处理记录 SQL 失败时事实和回执一起回滚；同位置不同字节拒绝。
9. DeleteRecords 制造保留缺口后，消费者重启明确失败，不静默跳过。

测试包含人为注入的应用/SQL/确认窗口故障，不等于已完成实际机器故障、Broker 切换或 PG 切主演练。
首轮发现未创建消费者组的清理返回 GROUP_ID_NOT_FOUND，已修正为仅接受成功或该明确不存在状态，清理残留后完整重跑通过。

## 迁移与清理证据

从 PG `platform.schema_migrations` 读取并与本地 SHA-256 核对，四个文件均匹配：

| 版本 | SHA-256 |
|---|---|
| 0001 | 0e5919d99f13509eab6eaac941c26b3b530fae5339f74419bd02f47932b1e40b |
| 0002 | f47abb1779e17f21fc8af2cc8ef219faf955c9e2e3b9e127d906c87f608c55e6 |
| 0003 | 6524d487f86ab5f65adb49de4d8b876c050a63125b0aba95b5358552cce88ae9 |
| 0004 | a7b68156bb9178f18cb4edd23a3085f4ee306536c4e9d211300d60bf54d684b1 |

0001～0003 未改动；新增 0004 的 applied_at 为 `2026-09-20T03:53:59.375Z`。
最终读取 crawler/control/ingestion 下全部 12 张表，行数均为 0；保留空表、约束和迁移台账。
Kafka 最终仅有 `crawler.results.validation.v1`、`crawler.publication.v1`、`__consumer_offsets`，listGroups 返回空组列表和空错误列表；临时测试 Topic/消费者组已清理。
旧参考仓库 `git status --short` 无输出，本次未改动旧仓库。

## 范围与后续

已实现可复用 Producer/Consumer 适配库、消息签名、公钥登记校验、事务接入、幂等重放和原文隔离。
当前业务范围为 incremental/delta、冻结的单个 VIDEO_METRICS 批次、1～100 项；尚未覆盖全部采集角色和完整计划结算。
下一步实现常驻服务、健康/积压指标与回执/隔离查询，落实签名凭据、Broker SASL/TLS/ACL 和最小 PG 权限后，再通过新仓库镜像与 Argo CD 部署验证。
Worker Local Journal、消息保留/积压告警、隔离回放流程、HA/备份恢复和生产容量仍须分别完成；本报告不以链路测试代替这些验收。
