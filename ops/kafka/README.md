# Kafka 运行检查与恢复边界

S1/S2/S3 各运行 Kafka 3.9.1 KRaft broker/controller。Ingestor、Connect、exporter与本仓库运维客户端现使用 `10.4.4.2:9094,10.4.4.8:9094,10.4.4.5:9094` 双向TLS入口，应用证书分别下发；operator私钥不进应用Pod。bootstrap 列表用于发现，之后客户端按 metadata 直连分区 leader；不能用只转发单地址的普通 TCP 代理替换所有 advertised 地址。

当前为三副本、默认 minISR=2。结果 Producer 使用 acks=all 和幂等生产。确认写入意味着满足 Kafka 副本确认条件，不代表 PG 已入库，也不代表已经获得异地备份。三成员 KRaft 仅容忍一个成员不可用；不在这个共享部署上同时停止两个服务做破坏性测试。

## 日常检查

```sh
node ops/kafka/check-health.mjs
```

该脚本只读：检查全部 Topic leader/三副本 ISR、验证消费者成员、每分区保留起点/已提交 offset/末尾 offset/lag。未知 offset 不伪装为 0；已提交 offset 落到保留起点之前会报错。不收集业务正文或凭据。

该脚本是一次性巡检，不判断 lag 的持续时间或生产吞吐目标。另已部署双 Prometheus、Kafka exporter、平台内告警与集中日志；参见 `ops/monitoring/`。KRaft quorum 另在任一健康 S 节点运行：

```sh
sudo -u kafka /opt/kafka/bin/kafka-metadata-quorum.sh --bootstrap-server 10.4.4.2:9094 --command-config /etc/kafka/tls/node-client.properties describe --status
```

连接失败时改用健康节点的 broker 地址。检查 leader、三个 voter 和 follower lag；还要通过系统监控检查进程、JVM、磁盘及网络。

## 受控单服务故障演练

```sh
node ops/kafka/verify-broker-failover.mjs --execute
```

该操作会短暂停止当前 KRaft leader 所在机器的 **Kafka 服务**，需要维护窗口；不会关机，也不停止同机 PG/etcd。首次运行需现有 Topic 全部分区三副本同步。使用唯一 UUID 的两份独立测试 Topic，分别 minISR=2 与 minISR=3；持续小消息收发、停止服务、验证 quorum/partition 接管、验证副本不足的写入不获确认、恢复 ISR、全量重读并核对正文与顺序。

minISR=3 测试是在仅停一台时人为提高专用测试队列要求，验证剩余两个 ISR 不满足要求。不等同于真实 minISR=2 队列已经演练过同时失去两台机器。Producer 可能返回超时而非直接暴露 Broker 的 NotEnoughReplicas；脚本同时核对正常队列可写、严格队列故障前可写及故障时末尾 offset 没有推进。

停服前设置远端 5 分钟 systemd 自动恢复 timer；finally 优先恢复 Kafka 再清理本次测试 Topic/Group。客户端被强杀后 timer 仍存在；运维需另外核对残留 `crawler.infra.*` Topic、Group 和对应 timer，再按运行 UUID 清理，禁止通配删除业务数据。证据写入 `ops/checks/kafka-failover-<uuid>.json`。

三机已安装 `20-controlled-stop.conf` 到 `/etc/systemd/system/kafka.service.d/`：由 systemd 向当前主进程发送 SIGTERM，允许 120 秒关闭，识别 Java 的退出码 143。避免上游脚本通过进程名匹配停止其他 Kafka，并避免正常维护产生 failed 状态。安装脚本也已同步相同设置。配置已纳入现有 etcd/主机配置备份范围。

本次证据只覆盖正常停止服务及恢复，不覆盖突然断电、整机冻结、磁盘损毁、网络分区、两个节点故障或容量压测。

## 保留与备份

| 队列 | 当前保留条件 | 边界 |
|---|---|---|
| crawler.results.validation.v1 | retention.ms=604800000；retention.bytes=134217728/分区；segment.bytes=16 MiB；segment.ms=1 小时 | 时间或大小任一条件触发即可清理；128 MiB 是验证期配置，不保证 7 天，删除按段且可能延迟 |
| crawler.publication.v1 | 继承 168 小时时间保留，默认无 retention.bytes 上限；segment.bytes=1 GiB | 预留正式队列，正式 Publication 尚未开发；不能把空队列作为容量验收 |
| crawler.publication.validation.v1 | CDC 验证队列，配置源 `ops/cdc/provision-topics.mjs` | 双 Connect / Debezium 已接通 PG failover logical slot；独立验证，不代表正式发布业务完成 |

结果消费者发现保留缺口必须明确报错并走恢复流程，不能直接跳到最新。Kafka 三份副本用于可用性，删除操作也会复制；PG 备份不包含 Kafka 中尚未入库的结果。现有 control backup 只覆盖 Kafka 配置，不备份实时日志目录。整个集群丢失时恢复未入库消息的路径尚未验收；Worker 结果重放/Journal 方案仍需单独完成，不引入 SQLite。

监控与运行验收需要持续覆盖以下边界（现有 exporter/主机探针与告警不等于全部完成）：

- 无 leader、ISR 低于 minISR、KRaft 失去多数派、备份失败：严重异常。
- 三副本下降为二副本：可用但降级，需及时恢复第三副本。
- 分区 lag、最老待处理消息年龄、输入/消费字节速率与预计保留余量：一起评估，不能只看消息条数或把三个分区平均。
- 磁盘剩余空间和增长速度、JVM 内存/GC、服务重启与生产失败率。
- 监控采样过期本身也是异常；全局采集准入需响应入库积压和保留风险，不能等数据删掉后才减速。

定时抓取及平台内告警触发/解除已验证；外部通知、生产阈值、最老待处理消息年龄和完整业务背压仍待。2026-09-21 已完成9094应用双向TLS迁移及应用9092出口移除；**broker副本复制9092、KRaft控制器9093仍明文，ACL尚未启用**，不能标记Kafka安全收尾完成。步骤和回滚见 [SECURITY-MIGRATION.md](SECURITY-MIGRATION.md)。消息签名、mTLS和应用网络策略各有职责，均不替代尚待配置的Broker最小授权。
