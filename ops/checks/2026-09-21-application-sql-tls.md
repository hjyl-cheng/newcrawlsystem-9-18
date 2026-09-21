# 应用数据库链路 TLS 上线与切主回归

日期：2026-09-21。承接 PG 服务端/复制/Grafana 的第一批加固。本批只修改部署配置、运维工具和探针；没有恢复真实采集、Planner 或业务 UI 开发，没有增加服务器、数据库实例或 SQLite。

## 完成范围

- Temporal 的 default/visibility 两个 SQL store 使用原生 TLS、专用 CA 和主机名验证；保持原两个逻辑库、账号与连接数预算。
- Debezium 使用 verify-full / sslrootcert；保留原 failover slot、offset、精确 publication、topic。Connect 两 Pod 先获得 CA，之后应用 connector 配置。
- 两 Ingestor 使用原生 pg + PGSSLMODE / NODE_EXTRA_CA_CERTS，经固定 DNS 验证 PgBouncer 身份；无业务代码/镜像修改。
- 三 Filer 使用 SeaweedFS 4.47 原生 sslmode / sslrootcert，从本机 15432 经 HAProxy 透传到当前主库 PgBouncer；逐台重启，旧连接排空后确认实际 TLS 连接。
- 三 PgBouncer 前端先 allow、迁完后 require；后端始终 verify-full 到同机 PG。保持原事务池参数、用户/库映射与上限；新增专用 PgBouncer 控制台账号，仅供受保护的本地 Unix socket 运维调用，没有给应用控制台权限或创建同名 PG 角色。
- 三 PG 的 HBA 顶部拒绝全部普通 SQL 和物理 replication 的明文 TCP，包含 IPv4/IPv6 规则；当前所有远程会话实测均加密。本机 Unix socket 保留原认证方式。
- 首次生成的 Filer/Temporal schema 配置及角色授权脚本同步保留 TLS 要求；旧 PgBouncer 整份重建脚本在检测到 TLS 接管后拒绝覆盖。已有 Temporal 库没有重新执行 setup-schema。

操作、信任关系、回滚与恢复见 `../postgresql-ha/APPLICATION-SQL-TLS.md`。没有新增端口、放宽 NetworkPolicy 或变更云防火墙。

## 实测

完整证据：`2026-09-21-application-sql-tls.json`。

1. 既有第一批边界测试继续通过：PG 正确信任、错误 CA/名称、Grafana 固定入口、复制/rewind 及本机 localhost 的 39 组真实连接。
2. 三台 PgBouncer 的节点地址、固定 Service 名称和回环名称验证通过。两个**真实 Ingestor Pod** 内的原版本 pg 客户端验证前端 authorized TLS 1.3、数据库后端 TLS；错误 CA、错误名称、明文连接均拒绝，事务级锁与协议级 named prepared statement 仍可使用。
3. 三 PG + 三 PgBouncer 共六个监听器，使用 crawler/Temporal owner 两种身份，共 12 次明文启动协议检查，均在交换密码前拒绝。实测连接使用 IPv4；IPv6 拒绝规则已配置，没有声称测试了未部署的 IPv6 网络。
4. S1 → S3 受控切主，再回到 S1，恢复严格同步一备；三个阶段均运行应用检查。记录的 28.78 秒包含切换、CDC 等待及完整应用探针，不是生产 RTO 或无错误切换承诺。
5. Ingestor 初始投递 3 条合法、3 条重复、3 条坏签名消息：合法入库，重复回执不变，坏签名隔离，权限/查询身份/提交后 offset 均通过。切主后和切回后各重放 3 条，原 3 份回执/事实不变，消费者追平。少量合成记录保留审计。
6. Temporal 三个阶段分别完成一个唯一合成工作流：每个 Activity 有一次受控失败后重试，timer 落盘后临时 Worker 停止/重建，最终结果通过新连接重查。只验证现有调度底座，没有部署真实采集 Worker。
7. 三个阶段都经过三 S3 网关做小对象写入、读取、列表、跨节点覆盖，并检查三 Filer 目录；测试对象随后删除。
8. 三阶段共 12 条确认提交的 CDC Outbox 事件全部到达 Kafka，本轮重复 0；数据库复制、guard 候选、Grafana 数据库/日志数据源/面板继续通过。
9. 三个阶段 PG 实际远程会话均 TLS，两个 Temporal 逻辑库都有真实连接；三个连接池的前端 require / 后端 verify-full 配置及真实前端 TLS 连接均核对。

临时 SQL Pod、cdc-admin-probe、端口转发和合成 Worker 均已清理。Kubernetes/Argo、监控和 CDC/Kafka 收尾状态见对应 `application-sql-*-health.json`。

## 边界与下一步

数据库服务端只强制加密，服务器身份校验在客户端执行；客户端身份仍由既有专用账号/SCRAM 和来源/数据库权限控制，没有改成所有客户端都需要证书。

Kafka 9092 仍是 PLAINTEXT；Temporal gRPC 和 Seaweed 内部 HTTP/gRPC/S3 HTTP 的认证加密尚未收尾。SQL TLS 不替代这些协议的认证，也不替代持续对象备份、证书维护、控制器整机接管、ClickHouse 磁盘/HA 与异地灾备。下一批优先迁移 Kafka 的客户端/节点通信和权限，仍不恢复业务功能开发。

收尾巡检：8 个 Argo 应用 Synced/Healthy；双 Prometheus 各 14 个 targets 全部 up，本地采集器/两个应用探针正常；CDC/guard/槽检查通过，Kafka 结果队列三个分区 lag=0。维护探针期间出现过诊断日志预算限流告警，按实际状态保留在监控证据中，不调高阈值或隐藏告警；不影响上述确认的业务探针消息。

最终告警复核：LogEventsDiscarded 已在两套 Prometheus 自动解除，仅保留既有 SeaweedContinuousBackupPending；未更改限流或告警规则。

## 收尾备份

- PG check/差异备份通过：`20260920-172412F_20260921-135220D`，仓库本次增量 3616640 字节。
- 控制面/配置加密备份 `control-20260921T055549Z.tar.gpg` 已保存 A1/S2，25529753 字节，源提交 `e923fb8`。
- 解密后核验全部 manifest 哈希、三台连接池 TLS/控制台凭据、Filer 当前和 pending 配置/CA、全局 HBA 明文拒绝，以及 operator 凭据副本，全部通过。见 `2026-09-21-application-sql-backup.json`；这是新增配置的备份覆盖核验，不声称又完成一轮全部数据库灾备演练。
