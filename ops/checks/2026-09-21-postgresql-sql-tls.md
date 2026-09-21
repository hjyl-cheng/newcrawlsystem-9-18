# PG SQL TLS 第一批上线与验收

范围：先完成外围安全配置，不继续业务开发。三台 PG 仍是原来的单逻辑数据库架构；原 Kubernetes/Argo、独立 Patroni/etcd、CDC guard 与备份继续使用。

## 实施结果

- S1/S2/S3 的 PG 5432 已使用专用 SQL CA 签发的独立服务器证书，最低 TLS 1.2，实测 TLS 1.3。SAN 覆盖各自私网 IP、节点名、localhost/回环地址及 `postgres-rw.crawl-validation.svc` 等稳定入口名。叶证书到期 2027-09-21，CA 五年。
- Patroni replication（包括物理 WAL、槽位同步、本机复制协议探测）和 rewind 客户端配置为 verify-full。三台 HBA 在原有来源/账号规则之前拒绝 replicator、patroni_rewind 的明文；未放宽既有权限。
- 两个 Grafana Pod 经 Argo 滚动到 verify-full，使用挂载的 SQL CA 验证固定入口身份。HAProxy 保持 TCP 透传。三台 HBA 均拒绝 crawl_grafana 的明文。
- CA 私钥仅保存在受保护 operator 目录，各节点只收到自身私钥；没有提交秘密，没有新增监听端口或防火墙放行。

## 实测证据

主证据：`2026-09-21-postgresql-sql-tls.json`；部署/回滚操作见 `../postgresql-ha/SQL-TLS.md`。

- 三台 PG 的 IP 和固定 DNS 证书验证均成功；错误 CA、错误主机名均被 TLS 客户端拒绝。
- 从三台 S 节点执行共 39 组真实连接检查：replicator 的 SQL/物理复制协议、rewind 的 SQL 登录，以及 replicator 的 localhost/127.0.0.1 检查。每组正确 TLS 登录成功，明文被 HBA 拒绝。没有把端口连通当成身份验证通过。
- Grafana 账号直接访问三台 PG，以及临时受限 Pod 经真实 Service DNS 访问固定入口：加密连接成功、明文拒绝。临时 Pod/NetworkPolicy 已清理。没有放宽生产 Ingestor 只准连 6432 的限制。
- S1 → S2 受控切主、再回到 S1；每个阶段都验证两个真实物理 walsender 的 TLS、备库 verify-full WAL receiver、Patroni 时间线、CDC guard 候选名单及 Grafana 数据库/日志数据源/两个面板。
- 三阶段共 12 条确认提交的隔离 Outbox 测试事件，Kafka 全部收到，本轮重复 0。首次切主到这一阶段检查完成为 18.41 秒，包含等待与验证操作，**不是生产 RTO 或零错误承诺**。
- 切主后的 CDC 只读健康检查通过，精确 publication、failover slots、候选屏障/guard 均正常；Kafka 三副本 ISR 和结果消费者健康另存 `2026-09-21-sql-tls-data-health.json`。
- 两套 Prometheus 各 14 个 targets 全 up、采集器成功、两个应用探针正常；8 个 Argo 应用 Synced/Healthy。复核时另观察到切主/认证测试期间日志预算限流产生的 LogEventsDiscarded 告警，不提高限流阈值或关闭告警；这属于允许丢弃的诊断日志，不是上述 CDC 事件丢失。已知 SeaweedContinuousBackupPending 继续保留，见 `2026-09-21-sql-tls-monitoring-health.json`。

## 演练中修正的问题

最初证书漏掉 localhost。跨机复制正常，但 Patroni 切主后的本机复制协议检查无法验证证书，时间线为空，受控切回被拒绝。已经同 CA/同密钥重签三台证书、reload，并加入真实回环连接和 Patroni 时间线验收；最终完整切主/切回通过。没有关闭证书检查、强行 promote 或修改 CDC 的晋升保护。

验收程序还修正了切换瞬间复制连接为空的等待处理。早期运维主机访问 Service 的测试受到原 NetworkPolicy 限制，最终使用带最小 egress 规则的临时 Pod；无新增长期访问例外。

## 未完成范围

Temporal、Debezium、Ingestor/PgBouncer、Seaweed Filer 的 SQL 通道尚未完成强制加密/身份验证迁移；部分本机 PgBouncer 后端可能已协商 TLS，但不能等同整段端到端 verify-full。Kafka 仍 PLAINTEXT，Seaweed 内部认证/TLS、证书自动轮换/到期告警仍待。本批没有演练真实时间线分叉后的 pg_rewind，也没有新增整机冻结/联合灾备验收。

持续对象备份、Argo controller 整机失联无人值守接管、ClickHouse 独立数据盘/HA、外部通知与异地灾备仍保留原边界。下一批继续迁移剩余 PG 调用方；不以本批结果宣称外围全部完成。
