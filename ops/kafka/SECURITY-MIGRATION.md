# Kafka 认证与加密迁移

本轮先增加 `SECURE` 双向 TLS 入口，再迁移调用方。当前仍处在兼容阶段，不能宣称 Kafka 已完成安全加固。业务开发继续暂停。

2026-09-21 后续实装：用户已放通9094，六机到三S的18条TCP路径通过。Ingestor、Connect（worker/producer/consumer/admin）和Kafka exporter各自使用独立证书，均已迁移9094；三份应用NetworkPolicy不再允许9092出口。运维/验证脚本使用本机operator证书。客户端阶段验收见 `ops/checks/2026-09-21-kafka-client-tls.md`；下列副本/控制器/ACL步骤尚未实施。

## 入口和身份

| 端口 | 节点 | 用途 | 第一阶段处理 |
|---|---|---|---|
| TCP 9092 | S1/S2/S3 | broker 副本复制及迁移期旧探针，PLAINTEXT | 应用已迁出，内部复制迁移后关闭 |
| TCP 9093 | S1/S2/S3 | 静态 KRaft 三成员选举，PLAINTEXT | 不修改，另行验证控制器迁移步骤 |
| TCP 9094 | S1/S2/S3 | 新 SECURE broker 入口，TLS 1.2/1.3、强制客户端证书 | 新增，先验收再使用 |

采用 Kafka 原生 mTLS 身份认证，无需另外引入账号数据库或 SASL 服务。服务器证书包含节点内网 IP 与节点名 SAN，节点证书含 serverAuth/clientAuth；客户端必须校验 CA 和服务器名称，不能关闭校验。客户端证书身份随后配合 Kafka StandardAuthorizer 的 ACL 限制主题/消费者组；mTLS 本身不等于已配置授权。

专用 CA 与操作端凭据：忽略目录 `secrets/kafka/pki/`（CA 5 年、节点/客户端证书 1 年）。主机仅下发本节点密钥和公共 CA，目录 `/etc/kafka/tls` 0700、文件 kafka:0600；CA 私钥和 operator 私钥不下发主机。应用阶段另签发ingestor/connect/monitoring三个clientAuth身份，通过 `prepare-client-tls.py --execute` 按命名空间下发Secret，挂载0440并配置fsGroup。默认 principal 为 `User:CN=crawl-kafka-<identity>`，当前尚无ACL限制其操作范围。

证书轮换/到期告警仍待接入；现有加密 control backup 已包含 `/etc/kafka` 与本地忽略 secrets，但每次变更后须验证新归档实际包含新增文件。

## 顺序与检查点

1. 运行 `python3 ops/kafka/secure-listener.py --execute`。只接受尚未启用 ACL、复制仍走 PLAINTEXT 的首阶段配置。逐台增开 9094，优先保留当前 leader 到最后。每次重启前核对全部服务、ISR3、quorum lag0；重启后通过旧入口等 ISR3/quorum 恢复，并向本机新入口发送真实 Kafka ApiVersions 请求验收 mTLS。Java AdminClient 在 bootstrap 后可能选择 metadata 中的其他节点，因此跨机新端口放通前不能把它当作仅本机入口探针。不要格式化存储、重建 topic 或删 offset。
2. 运行 `python3 ops/kafka/verify-secure-listener.py`。三机分别验证 TLS1.2/1.3 下 Kafka ApiVersions、错误 CA/错误名称/无客户端证书/不受信任客户端证书拒绝；六台主机各检查三 S 的9094。随后还须从实际 Pod 验证 NetworkPolicy、路由及 metadata 返回的全部地址。
3. 如 9094 跨机被拦截，轻量云防火墙新增 TCP9094、来源 `10.4.4.0/22`，应用到 S1/S2/S3。不需要为 A 节点新增该入站规则，也不应开放公网来源。放行前保留现有应用入口。
4. 独立签发应用身份，下发 Secret；迁移 Ingestor、Connect 内部客户端/source producer、exporter、运维/测试工具与 NetworkPolicy。Java 使用 PEM 文件配置；Confluent Kafka JavaScript 1.10.1 使用其 librdkafka 原生 `security.protocol`/`ssl.*.location` 配置，不把普通 KafkaJS TLS object 生搬过来。源码变更走固定 digest 镜像和 Argo CD。
5. 全部 broker 的新入口可达后才滚动迁移 `inter.broker.listener.name=SECURE`，逐节点验证复制。控制器9093的认证/加密须另行验证静态 quorum 迁移，不能直接在同端口混用 SSL 和 PLAINTEXT；也不能把通用 broker 滚动升级文档当作控制器无缝迁移证明。
6. 配置 StandardAuthorizer、必要内部身份与最小 ACL；确认各身份正常操作/越权拒绝，再关闭9092。禁止以匿名超级用户或 `allow.everyone.if.no.acl.found=true` 作为最终配置。未解决控制器通道前不得把整套通信标记为安全。
7. 小消息收发、重复入库、CDC 确认事件、监控与单服务恢复验收；更新证据、备份和总计划。

## 全调用方清单与预期权限（尚未生效）

| 调用方 | 配置源 | 独立身份与范围 |
|---|---|---|
| Data Ingestor | `services/data-ingestor/src/main.ts`、`deploy/base/data-ingestor/` | 结果 topic Read/Describe；仅指定 group Read |
| Kafka Connect / Debezium | `deploy/base/kafka-connect/worker.properties`、`ops/cdc/connector.json` | 三个内部 topic Read/Write/Describe、Connect group Read；验证发布 topic 与心跳 topic Write/Describe，必要幂等权限 |
| Kafka exporter v1.10.0 | `ops/monitoring/render.py` → `deploy/base/monitoring/resources.yaml` | 监控所需 Describe/DescribeConfigs/组查询；不授予业务 Write |
| 本机运维/验证 | `ops/kafka/*.mjs`、`ops/cdc/{consume-probe,provision-topics}.mjs`、`ops/ingestor/verify-*.mjs` | operator仅受控运维；测试 Producer/Consumer 另设身份并限定验证 topic/group |
| 自动化测试 | `test/kafka/` 及环境指定 brokers 的调用方 | 与生产身份分离；只有所需验证队列权限 |
| 主机监控探针 | `ops/monitoring/collect-local.py` | 当前只检查 kafka systemd 服务，不连接 Kafka 协议；无需 Kafka 私钥 |

Connect 必须同时配置 worker 自身内部读写/admin 和 `producer.*`，不能只改 bootstrap 地址。保留原 source offset、`crawl_cdc_validation` 槽与 `cdc_validation.outbox` publication；不能重建 connector 来回避迁移问题。受保护的 PG SQL TLS 参数也不得回退。

## 第一阶段回滚

每节点原配置保存在 `secrets/kafka/migration/<uuid>/`。修改前主机创建8分钟回滚定时器，恢复该节点修改前配置并重启；检查失败时立即同样回滚，恢复后停定时器，绝不继续下一台。正常通过 quorum/ISR 检查后停定时器并删除主机临时回滚文件。脚本被强杀时先核对定时器、实际配置和进程状态，再继续。

手动回滚仅限第一阶段：确认没有调用方或节点复制依赖9094，按节点将对应快照还原到 `/etc/kafka/server.properties`（kafka:0640），重启该节点，等待全部 ISR3 和 quorum lag0，再继续。不能把这份旧快照用于后续已经关闭9092/启用ACL的阶段。新 PKI 文件可保留，避免重新签发覆盖身份。

旧 `install-kafka-kraft.sh` 现为全新安装专用，检测到既有配置或 meta.properties 即退出，防止覆盖安全配置或存储身份。

## 官方依据

- [Kafka 3.9 增量接入安全特性](https://kafka.apache.org/39/security/incorporating-security-features-in-a-running-cluster/)：增开安全端口→迁客户端→迁 broker 复制→关闭旧入口；每次等 ISR 恢复。
- [Kafka 3.9 SSL/PEM 与双向认证](https://kafka.apache.org/39/security/encryption-and-authentication-using-ssl/)：支持文件式 PEM、服务器名称验证及 required 客户端证书。
- [Kafka 3.9 Listener 配置](https://kafka.apache.org/39/security/listener-configuration/)：broker 和 KRaft controller listener 用途分离。

实际完成程度以 `ops/checks/2026-09-21-kafka-mtls-*.json` 和验收记录为准，本手册后续步骤不是已部署声明。
