# PostgreSQL 固定入口与自动故障切换验收

日期：2026-09-20。范围仅外围基础设施；没有新增业务服务代码，没有修改旧仓库，没有引入 SQLite/Rota/Resin。

## 实际部署

| 位置 | 已部署内容 |
|---|---|
| S1 10.4.4.2 | PostgreSQL 17.11、Patroni 4.1.5、独立 PG etcd 3.5.24 成员、PgBouncer 1.25.2 |
| S2 10.4.4.8 | 相同 PG/Patroni/etcd/PgBouncer，另保留加密 pgBackRest 仓库与定时器 |
| S3 10.4.4.5 | 相同 PG/Patroni/etcd/PgBouncer，独立恢复验证目录已停止 |
| A 节点 Kubernetes | 两个跨节点 HAProxy 3.2.13 Pod；ClusterIP `postgres-rw`；由新仓库 Argo CD 管理 |

应用路径：

```text
Data Ingestor (两个 Pod)
    → postgres-rw:6432 → HAProxy → 当前主库所在 S 节点的 PgBouncer → PG
Temporal (两个 Pod)
    → postgres-rw:5432 → HAProxy → 当前主库 PG
                                ↑
                    HTTPS/mTLS :8008 /primary
                                ↑
                 S1/S2/S3 Patroni ↔ PG 专用 etcd 多数派
```

固定地址 `postgres-rw.crawl-validation.svc` 仅供集群内使用，不是给浏览器访问的公网地址。8008 为 Patroni 健康/管理接口；云防火墙已验证 A1/A2/A3 到三 S 节点，以及三 S 节点相互可达。2379/2380 为专用协调通信。

保留既有全部数据库和数据目录，未执行 initdb/reclone。原 `postgresql@17-main` 已 mask，`start.conf=manual`；由 `crawl-patroni` 唯一管理。初始配置受保护备份在各 S 的 `/srv/crawlsystem/pg-ha/pre-patroni/main`。

验收后：S1 Leader，S2 Sync Standby，S3 Replica；timeline 5，两条复制 streaming、采样 lag 0。五个 Argo Application 均 Synced/Healthy。

## 核心参数与边界

- 一套逻辑数据库、不分片；严格同步至少一台备库，正常情况下已确认提交需主库与同步备库存储确认。两备库皆失联时会阻塞写入。
- DCS TTL 30s，loop_wait/retry_timeout 各 5s；softdog required、20s 超时。正常只有主库 watchdog active，备库 inactive。软件看门狗不等于云侧硬件 fencing；本次没有通过冻结进程强制重启整机，不声称所有故障类型已验证。
- PG `wal_log_hints=on`，支持受控 pg_rewind；自动删除分歧数据目录/自动重建关闭。不是每次切主都必然调用 rewind。
- PG etcd 使用独立 CA、双向 TLS 与认证；Patroni 用户仅授权 `/service/crawl-pg/` 前缀。实际验证该前缀可读、其他前缀拒绝。
- REST 使用另外的 CA；健康证书无法单独执行 reload（401），写操作另需管理账号密码。etcd HTTP gateway 对带 CN 的客户端证书有兼容限制，最终使用无 CN 证书加账号认证。
- HAProxy 镜像锁定 digest `sha256:5314c30b9908c0edc608eb9ae99bb1c1a4fcb9e52cebbea8f5ec3df128fc7f04`，健康检测 2s、连续两次成功/失败，旧主失效关闭旧连接。
- Node pg 8.23.0 + PgBouncer 1.25.2：协议级 named prepared statement、事务级 advisory lock 已验证。statement_timeout/idle_in_transaction_session_timeout 启动参数由池忽略，但在账号/数据库级强制同等 30s/45s 默认值；实际 SHOW 已核对。首次滚动期间因此产生的失败已修复，没有变更应用镜像。

## 故障与数据验收

| 实验 | 结果 |
|---|---|
| 受控 S1 → S2 切换 | 约 2.15s 观察到新主；写入最大间隔 4.847s；257 条确认记录在三节点均存在 |
| S2 到三个 DCS endpoint 网络隔离 | 约 10s 后旧主不再报告可写；约 31.09s 观察到 S3 新主；恢复网络前 SQL 确认 S2 已只读；写入最大间隔 25.367s；176 条确认记录三节点完整 |
| 恢复 S2 网络 | 临时规则清理，旧主自动恢复 streaming；没有人工 promote 或删除数据 |
| 停 S1 PG etcd 成员 | S2/S3 quorum health 成功、S3 PG 仍为主；启动 S1 后三成员均 healthy |
| 删除一个 HAProxy Pod | Service 继续写入，替补自动就绪；20s 内确认 78 条记录、全部保留；这不是所有活跃连接必定无中断的证明 |
| 既有入库服务 | 权限、3 条 APPLIED、重复幂等、3 条无效签名隔离、Kafka offset/metrics 验证通过；故障后重放 3 条，回执与计数保持不变、消费追平 |
| Temporal | 换主后合成任务、两次预期 Activity 重试、Worker 重建和持久结果重新查询通过 |
| 最终回归 | S3 → S1 正常切回，S2/S3 streaming，应用恢复 2/2 Ready |

原始测量摘要见同目录 `2026-09-20-pg-switchover.json`、`2026-09-20-pg-dcs-partition.json`、`2026-09-20-pg-entry-replacement.json`。测试只向唯一临时 schema 写少量记录，最终已清理。角色采样不等于连续无缝观测；以上 RTO/写入间隔仅针对本轮小数据验证，不是生产 SLO。确认记录未丢失不意味着客户端永远不会遇到提交结果不明，上层仍需回执与幂等。

隔离演练的首次注入因 `/run` 挂载 noexec 未执行，未切断网络；改用 `/bin/sh` 执行受保护的自动恢复脚本后完成实测。90 秒自动撤销与 finally 撤销双重清理，不修改 SSH、PG、Kafka 或既有防火墙规则。

## 备份与恢复连续性

- 接管前差异备份 `20260920-172412F_20260920-180022D` 完成。
- 仓库现在发现三节点；各 PG 用 `/etc/pgbackrest/pg-node.conf`。S2 同时作为仓库主机和 PG 主机时，SSH 身份、锁目录和配置相互分开。
- S2 当主时归档/差异备份 `20260920-172412F_20260920-181436D` 成功；S3 当主时 `20260920-172412F_20260920-181916D` 成功。归档有 timeline 3/4 的实际 WAL 证据。
- 用 timeline 4 的备份恢复并跨 timeline 5 回放到 `crawl_restore_8f1de03ec3704128`：before 存在、after 被排除、全部回执 ID/提交 ID/hash 匹配、六个数据库完整。S3 独立目录 `/srv/crawlsystem/restore-check-8f1de03ec3704128` 已停止；不监听 TCP、不覆盖在线数据。
- 控制备份新增 PG 专用 etcd 快照、Patroni/协调证书及配置。`control-20260920T100829Z.tar.gpg` 的 9 份成员文件校验成功；Kubernetes registry/Secrets/Deployments 及独立 PG etcd 的 9 个协调键可恢复，严格同步配置和认证状态保留。
- S2 仍是唯一 PG 备份仓库；本轮不是备份仓库 HA 或异地灾备。监控/告警和跨故障域备份仍待完成。

## 24.4 差异与下一项

本次物理 PG HA 与稳定入口已实施；24.4 的 Debezium logical failover slot/CDC 还没有实现，目前 wal_level=replica，不存在 Debezium Connector，不能宣称 Publication 已 HA。Temporal 单独使用会话通道，符合不能强行套事务池的约束。现场以专用 systemd 服务实施 Patroni/etcd，而非引入整套 Pigsty；稳定入口用 Kubernetes Service/HAProxy，避免假设轻量云支持 VIP。

下一外围项：Kubernetes API 的稳定入口与 Argo CD 可用性；随后按 24.12 处理 Kafka/SeaweedFS/ClickHouse、CDC、监控告警和安全加固。业务代码继续暂停。


最终收尾复核（同日）：完整新版 Ingestor 已通过连接池重新执行 3 条新结果、3 条重复、3 条隔离消息验证，各分区 offset 到 57 并追平。最终控制备份 `control-20260920T102351Z.tar.gpg` 在 A1/S2 校验一致，独立恢复 9 份文件、Kubernetes registry 667 / Secrets 11 / Deployments 12、PG 协调键 10，认证保留。临时 Pod、端口转发和故障注入规则已清理；所有服务与备份定时器正常。三台 S 当前可用内存约 5.3～6.2 GiB（小数据空闲状态采样，不作为负载容量承诺）。
