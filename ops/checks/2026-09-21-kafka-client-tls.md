# Kafka 应用双向 TLS 迁移验收

2026-09-21：用户放通 TCP9094 后，六机到三S的18条路径全部可达。三类应用与运维客户端已迁移至9094；**内部broker复制9092与KRaft控制器9093仍为明文，ACL尚未启用**，本轮不将Kafka整体安全标记为完成。

## 已部署

| 客户端 | 身份 / Secret | 实际部署 |
|---|---|---|
| Data Ingestor | `CN=crawl-kafka-ingestor` / `crawl-validation/kafka-ingestor-tls` | 两Pod，librdkafka原生SSL配置、校验CA和服务器名称 |
| Connect / Debezium | `CN=crawl-kafka-connect` / `crawl-validation/kafka-connect-tls` | 两Pod，worker/producer/consumer/admin全部SSL；PEM文件，无密码参数出现在命令行 |
| Kafka exporter | `CN=crawl-kafka-monitoring` / `crawl-monitoring/kafka-monitoring-tls` | 两Pod，启用TLS及客户端证书，不关闭服务端校验 |
| 运维、消息验证与Kafka测试 | `CN=crawl-kafka-operator` / 本机忽略PKI目录 | `ops/kafka/client.mjs`统一原生TLS参数，三bootstrap均9094；不下发operator私钥给应用 |

Secret挂载0440、应用fsGroup读取，根CA私钥只保留在受保护操作端/加密备份。通过固定digest和原Argo应用滚动更新，未改业务数据库设计、CDC publication/slot/connector名称、内部topic或消费者组身份。

TLS源码提交 `863833f2b650d075b98d758fda27b97e9525143b`；Runtime CI `35574494025` 成功。Ingestor固定镜像：

```text
ghcr.io/hjyl-cheng/newcrawlsystem-runtime@sha256:dedbc97c99a60d44596c0eb8b45f4109c3068e429389e0827dbc1617035f9dd9
```

发布清单 `bcb64e6`；应用9092出口收口提交 `2200bfc`。监控通过 `ops/monitoring/render.py`生成，源码与生成清单保持一致。三份应用NetworkPolicy仅允许Kafka9094，迁移阶段暂留的9092出口已移除。

## 证据

- `2026-09-21-kafka-mtls-pre-firewall.json`保留原跨机阻断；`2026-09-21-kafka-mtls-boundaries.json`记录放通后18条路径成功及三机本地证书拒绝检查。
- `2026-09-21-kafka-client-tls.json`：六个真实Pod仅建立9094 Kafka连接，没有9092连接，挂载的客户端证书逐一与各自身份核对。两个Ingestor分别对三Broker执行原生SDK metadata请求/真实TLS Kafka API，并验证错误CA、错误名称、无客户端证书被明确拒绝；六条旧9092路径被网络策略阻止。Connect四类客户端SSL属性及exporter校验参数核对通过。
- `2026-09-21-kafka-client-chain.json`：3合法入库、3重复幂等、3坏签名隔离；PG确认的4条新CDC事件全收，重复0；消费追平，Connect、PG slot和CDC guard正常。这次操作端和实际应用均已走mTLS。
- 50项无外部依赖测试/CI通过；真实Kafka/独立测试PG库9项测试在9094上通过，包含并发分区处理、事务回滚、重放与保留缺口拒绝。
- `2026-09-21-ingestor-kafka-tls-reconnect.json`：定向停止结果消费组当时的协调节点Kafka服务，检查两份Ingestor Pod UID及restartCount，故障期间和恢复后分别执行3合法/3重复/3坏签名回归。最终状态、采样时间与恢复记录以JSON为准，不据单次采样声明固定生产RTO。
- 最终监控状态见 `2026-09-21-kafka-client-monitoring.json`；加密control备份、A1/S2密文一致性、解密后的PKI/配置覆盖见 `2026-09-21-kafka-mtls-backup.json`。

滚动迁移同时暴露并修复旧Ingestor底层客户端断言崩溃，详见 `2026-09-21-ingestor-kafka-reconnect.md`。原失败记录保留；不能用最后一次通过掩盖此前的重启或短暂不就绪。

## 回滚和剩余工作

当前旧9092服务器入口仍供broker复制使用。客户端需要回滚时，先通过Git重新允许对应应用9092出口，再原子回退其bootstrap/证书配置与Ingestor旧镜像至一致版本；滚动时保留已验证的新旧通道直到全部调用方健康。单独把旧镜像指向9094会因协议不匹配失败，不能这样回滚。Connect必须保留原topic/offset/slot，不能删除重建。

下一阶段先迁移broker副本通信和KRaft控制器通道，再配置StandardAuthorizer与最小ACL，完成应用越权拒绝检查并关闭旧监听。Kafka 3.9.1源码支持多个controller listener、出站使用首个listener；兼容迁移仍须单独实测静态voters/监听端口变化，不能直接在9093同端口滚动混用明文和TLS。

证书轮换/到期告警与跨集群灾备未在本阶段实现。Seaweed、Temporal、Argo controller、ClickHouse及持续备份的既有外围待办继续保留；没有恢复Planner/真实Worker等业务功能开发。
