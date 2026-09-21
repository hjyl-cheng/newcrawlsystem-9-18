# Kafka 加密入口第一阶段验收

日期：2026-09-21。仅新增兼容入口，**尚未完成 Kafka 安全迁移**。

## 实际变更

- S1/S2/S3 的 Kafka 3.9.1 均增加私网 TCP9094 `SECURE:SSL` listener，强制客户端证书，支持 TLS1.2/1.3，验证服务器 CA/名称。
- 独立 Kafka CA、三节点和 operator 身份；主机只有自身私钥和公共 CA，未下发 CA 私钥/operator 私钥。应用专属证书尚未下发。
- 保留9092应用/副本通信及9093静态KRaft通道，未开启ACL，未改Connect offset、PG slot/publication。新增listener后发现Ingestor原生客户端切换崩溃，另行修复并发布，见下文。
- 加入逐节点滚动、健康门禁、主机8分钟自动回滚；旧 bootstrap 遇既有配置/存储即退出，防止覆盖。
- 全部修改来自新仓库，旧仓库未动；没有引入新数据库或 SQLite。

## 验证结果

1. 每次操作前后三个服务正常、67个分区均RF3/ISR3、quorum follower lag0；三机重启完恢复健康。详见 `2026-09-21-kafka-mtls-listener.json`。
2. 三机本机新入口分别完成TLS1.2/1.3下的Kafka ApiVersions请求；错误服务器CA、错误服务器名称、无客户端证书、不受信任客户端证书均收到证书/TLS明确拒绝，不将超时视为认证拒绝。详见 `2026-09-21-kafka-mtls-boundaries.json`。
3. 六机到三S的18条TCP路径中，三条同机成功、15条跨机全部超时；各主机UFW均未启用。新listener本机正常，跨机网络仍待放行，因此未迁移应用或副本流量。
4. 原入口兼容性回归：3合法入库、3重复幂等、3坏签名隔离通过，消费者追平；PG确认提交的4条独立CDC事件全收，重复0；connector/task及guard/slot正常。详见 `2026-09-21-kafka-listener-existing-chain.json`。此项走原9092，不能声称已经验证加密应用链路。
5. 双Prometheus各14个targets全部up。滚动时Kafka日志产生短时预算丢弃告警；这是既有有界诊断日志策略，须区别于Kafka消息丢失。最终告警状态见 `2026-09-21-kafka-listener-monitoring.json`。Seaweed持续备份待办继续保留。

后续核对监控发现两份Ingestor曾各重启3次；底层librdkafka断言崩溃，不能把“Pod自动重启后回归通过”当作无崩溃接管。已改用Consumer.committed读取自身消费组进度，发布与定向单协调节点停服验收另见 `2026-09-21-ingestor-kafka-reconnect.md` 和JSON；原始运行记录仍保留。

首次S3迁移使用Java quorum CLI检查新入口失败，自动恢复原配置且确认集群健康；原始失败记录保留在 `2026-09-21-kafka-mtls-first-attempt.json`。现场当时listener已启动，但新端口跨机不通，Java AdminClient可能在bootstrap后访问其他advertised节点。改用明确指向本机socket的Kafka API检查，集群quorum/ISR仍由旧入口独立核对，再次部署通过。没有将原失败删除或当作已验证的跨机TLS。

所有本轮回滚timer已取消、主机临时回滚脚本已清理；受保护的原配置快照保存在忽略目录。PKI/配置已纳入既有control加密备份范围，最新备份与解密核验结果另记 `2026-09-21-kafka-mtls-backup.json`，不把配置备份当作Kafka消息备份。

## 下一步及防火墙

在轻量云防火墙模板增加 **TCP9094，来源10.4.4.0/22**，应用于：

| 节点 | 公网 | 内网 |
|---|---|---|
| S1 | 43.172.65.165 | 10.4.4.2 |
| S2 | 43.159.169.76 | 10.4.4.8 |
| S3 | 43.172.80.48 | 10.4.4.5 |

无需给A节点新增9094入站；不要删除9092/9093规则。放通后重新验证18条主机路径，再迁移实际Pod、NetworkPolicy、受限身份和运维脚本，之后处理复制、控制器协议与ACL并关闭旧入口。完整阶段、调用方清单和回滚见 `ops/kafka/SECURITY-MIGRATION.md`。

本轮未做压力测试或整机灾备，不宣称外围已经全部完成。Temporal gRPC、Seaweed内部协议、证书维护、持续对象备份、Argo controller整机接管、ClickHouse磁盘/HA和外部灾备等待办仍在。


后续更新：用户已按上述规则放通9094；新网络验收全部通过，三类应用已完成mTLS迁移及旧端口出口限制，最新事实见 `2026-09-21-kafka-client-tls.md`。上文“等待放行”保留为第一阶段历史，不再是当前阻塞项。
