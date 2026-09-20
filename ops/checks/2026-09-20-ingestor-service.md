# Data Ingestor 常驻服务验收

日期：2026-09-20。当前记录：代码/本机真实依赖验收已通过，GitOps 部署结果将在完成后补录。

- 使用独立 crawler_validation_ingestor，0001～0004 已应用；正式 crawler/Business 未改。
- 受限账号 crawler_ingestor_validation，pool=4，禁止控制权修改、DDL、删回执、改非切片字段和连接正式 crawler；拒绝项在真实 PG 上逐项验证。
- 三个分区均通过合法提交、重复提交、坏签名隔离和 offset 持久化检查；回执接口的身份/频道权限、隔离元信息查询与 metrics 通过。
- 首轮每分区 offset 0～2，3 个 APPLIED 业务回执、6 个 APPLIED 消息记录和 3 个隔离记录；重复消息没有新增业务回执。
- 27 项无数据库测试通过，YAML 服务端 dry-run 通过；本机收到 SIGTERM 后正常 draining/stopped，退出码 0。
- 凭据只存在忽略目录和 Kubernetes Secret；消费者没有签名私钥或迁移 owner 密码。
- Kafka 沿用内网 PLAINTEXT，生产 Broker 认证/加密/ACL 未完成。指标尚未接自动抓取告警，真实 Worker/Temporal 未上线。

运行、构建、查询和回滚说明见 `ops/ingestor/README.md`。
