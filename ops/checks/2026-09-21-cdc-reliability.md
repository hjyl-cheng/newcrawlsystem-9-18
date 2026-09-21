# CDC 基础设施与受控切换验收

2026-09-21。仅使用隔离探针，业务代码继续暂停，旧仓库未修改。

## 已实测

- 三 PG17 节点启用 logical WAL、原生 failover slot 同步，保留严格同步 1 备库。切主后最终 S1 主库、S2/S3 备库，所有 nofailover 标记已恢复 false。
- Kafka Connect 跨 A 节点两个副本，由 Argo 管理。Debezium 3.6.3.Final、实际 Connect 4.3.0 与现有 Kafka 3.9.1 联通。PostgreSQL Connector 单任务 RUNNING。
- 只发布 `crawler` 数据库中的 `cdc_validation.outbox`，同时写 excluded_noise 而不纳入 publication；无自动创建 publication、无全表订阅。实际 Kafka inventory 没有原始 CDC/噪声表 topic。
- 五个本轮 topic 的实际分区、副本、minISR、cleanup 和保留配置已核对；Connect config/offset/status 写入 Kafka，非 Pod 本地文件。
- 完整成功批次：健康态 4 条，受控删除活动 Connect Pod 后累计 8 条，PG S2→S1 计划切主后累计 12 条，确认 S1 为主后累计 16 条；16 个唯一事件全部收到，消息 key 与事件 ID header 匹配，本轮观察重复数 0。并不证明 exactly-once，生产消费端仍须幂等。
- Connect 任务 worker 变化观察耗时 3.62 秒；PG 计划切主及 Connector 就绪观察耗时 15.72 秒。这是小数据维护测试计时，不是生产 RTO。事件在各阶段写入，不是持续故障期间的高并发或未知提交结果测试。
- 本轮前一次演练 S3→S2 时，12 条已确认事件全部收到，但旧主 S3 留下的非同步槽挡住了备用槽同步，完整演练因此停止。先设置 nofailover 排除 S3，再受控删除其 inactive/nonsynced 本地槽；正常探针提交和 CHECKPOINT 推进解码，永久同步槽追上后恢复晋升资格。没有删除主库槽、强制跳 LSN 或把失败演练记成全自动成功。
- 只读 CDC/Kafka 健康检查通过；7 个 Argo 应用 Synced/Healthy，6 个验证 Deployment 均为 2/2。
- 切回 S1 后 pgBackRest check/WAL 归档通过，新差异备份 `20260920-172412F_20260921-104251D` 成功，13.1 MB；这不是 CDC 联合灾备恢复证明。
- 恢复脚本增加受保护断点记录及只检查/恢复标记的 resume 分支。5 项隔离安全测试通过：不修主库、不删已同步槽、resume 不删槽、失败保留记录、临时槽不恢复晋升。未为了测试新分支再次破坏现场槽。

## 明确未完成

CDC 当前等待两台指定备库，其中一台失联会暂停投递，尚无动态安全候选调整；旧主槽冲突的处理是操作员流程。**不能标为整机故障下完整无人值守 CDC HA。**

未实现正式 `publication.outbox`、Business Consumer Inbox/Apply 或外部 Business DB 交付。未验证长时断连/槽超限恢复、空闲捕获库的 WAL 推进、PG+Kafka 联合恢复、持续告警或生产认证/TLS。2GB 槽保留设置在检查点生效，不是 pg_wal 总大小硬上限。

与 24.4 的关系：本次落实 logical failover slot 技术基础；精确 outbox publication 的原则保留，因业务暂停而使用独立运维探针表。自动故障接管及业务投递仍属于未验收部分，不能用物理 PG 高可用代替它们。其他外围遗留事项继续见 24.12。

## 证据

- `2026-09-21-cdc-pg-prepare.json`：参数和槽归属。
- `2026-09-21-cdc-connector.json`、`2026-09-21-cdc-topics.json`：版本、Connector、实际 Kafka 配置。
- `2026-09-21-cdc-chain-failover.json`：成功批次、事件、阶段 offset 和三个节点槽位。
- `2026-09-21-cdc-demoted-slot-repair.json`：首次失败后的人工受控恢复与重新纳入晋升。
- `2026-09-21-cdc-health.json`、`2026-09-21-cdc-kafka-health.json`、`2026-09-21-cdc-final-state.json`：收尾健康及切主后备份。

运行/恢复步骤见 `ops/cdc/README.md`。下一阶段接集中监控与告警，在界面可见地覆盖复制、槽保留 WAL、Connect 状态、Kafka 积压及备份新鲜度；其余可靠性和安全遗留继续跟踪，业务代码仍暂停。

收尾配置备份：`control-20260921T024837Z.tar.gpg`，A1/S2 两份密文 SHA256 一致（`ba6f5f466fd3556d99f69333398aa706951fbb464180bb3619cdb7fd78b45697`），源码版本 `74fa1db`。包含当前 etcd、六节点配置和受保护凭据，不代替 Kafka 数据/offset 灾备；证据见 `2026-09-21-cdc-control-backup.json`。
