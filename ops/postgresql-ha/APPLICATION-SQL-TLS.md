# 剩余应用 SQL 通道加固

这是 `SQL-TLS.md` 的第二批，保持原服务器、数据库、端口和服务身份，只迁移连接配置。客户端必须校验 CA 和服务器名称，数据库/连接池服务端强制加密。业务功能与采集算法没有修改。

## 链路与配置

| 链路 | 配置 |
|---|---|
| Temporal 两个 SQL store → postgres-rw:5432 → 当前 PG 主库 | 原生 tls.enabled / enableHostVerification / caFile |
| Debezium → postgres-rw:5432 → PG | database.sslmode=verify-full、database.sslrootcert；保留槽、offset、publication |
| Ingestor → postgres-rw:6432 → 当前 PgBouncer | 原生 pg 的 PGSSLMODE=verify-full，NODE_EXTRA_CA_CERTS 载入 CA |
| 三 Filer → 本机 127.0.0.1:15432 → 当前 PgBouncer:6432 | SeaweedFS 4.47 原生 sslmode=verify-full + sslrootcert；HAProxy 透传；证书含 127.0.0.1 SAN |
| PgBouncer → 同机 127.0.0.1:5432 | server_tls_sslmode=verify-full、server_tls_ca_file |
| 运维 Node/libpq/Temporal schema 工具 | 保护目录中的 CA + verify-full；owner 与运行身份分开 |

复用上一批 SQL CA。PgBouncer 和本机 PG 都由 postgres 用户运行，复用本节点 SQL 身份证书/私钥；不把 CA 私钥下发。Filer 只取得 CA。Kubernetes 应用挂载 crawl-validation 中的 postgresql-sql-ca Secret。PGSSLMODE 控制 Node pg 的 SSL；纯 JS pg 不读取 PGSSLROOTCERT，因此通过 Node 原生 NODE_EXTRA_CA_CERTS 在进程启动时载入 CA，没有设置 rejectUnauthorized=false。

本批只加固 SQL 连接。Temporal gRPC 仍是原来的受限内网无业务认证配置；Kafka 9092、Seaweed 内部 HTTP/gRPC 仍待后续独立加固，不能把 SQL TLS 等同整个系统已加密。

## 可重现部署顺序

1. 确认上一批 SQL 服务端/复制/Grafana TLS 已完成，三 PG 健康。运行 `python3 ops/postgresql-ha/secure-application-sql.py prepare --execute`。
2. 脚本把迁移前的 PgBouncer、userlist、Filer、HBA 保存到忽略的 secrets/postgresql-ha/sql-client-migration，写入应用 CA Secret。PgBouncer 前端先设 allow（已是 require 时不降级），后端改 verify-full；reload + RECONNECT 让后端空闲连接重建，不重启 PG。
3. 单独生成 `crawl_pool_operator` PgBouncer 控制台身份，只在受保护 userlist 和 operator secrets 内保存随机凭据，不创建 PG 数据角色、不下发到应用。用于 SHOW CLIENTS/SERVERS/CONFIG 和 RECONNECT，通过本机 Unix socket 使用；控制台权限不交给监控/业务。
4. 推送 Temporal、Ingestor、Connect 的部署清单，由 Argo 滚动。Connect 这一步只取得 CA，不同时更换槽/Topic。
5. 两个 Connect Pod 均挂载 CA 后，运行 `python3 ops/cdc/connect-admin.py` 应用仓库中的 connector.json，并检查 connector/task RUNNING；完成后删除临时 cdc-admin-probe。该配置变更通过既有 REST 管理入口应用，Argo 不直接管理 connector REST 资源。
6. 运行 `python3 ops/postgresql-ha/secure-application-sql.py seaweed --execute`，逐台更新当前与 pending Filer 配置，逐台 restart，验证本机 Filer 目录和主库 PgBouncer 中真实 TLS 连接后再操作下一台。默认 systemd 最长正常停服窗口 90 秒，期间另外两台继续服务，不把长连接退出慢当成需要三台一起重启。
7. 核对每个应用实际连接和小数据业务探针正常，再运行 `... enforce --execute`。该阶段拒绝在还有明文会话时收紧：PgBouncer 前端改 require，PG HBA 顶部拒绝所有 IPv4/IPv6 的普通 SQL 和 replication 明文连接。本机 Unix socket 继续使用原账号/peer 规则。
8. 运行 `python3 ops/postgresql-ha/verify-sql-tls.py --application-clients --execute`：完成真实客户端、明文/错误 CA/错误名称拒绝、事务池行为与应用回归，并在 PG 受控切主/切回后再验证各链路。只用已有合成验证库、Outbox、对象桶，不执行真实采集，不动外部业务库。
9. 生成新的 PG 差异备份与 control 加密备份；验证新增配置/控制台凭据/CA 在备份中。

## 运维与回滚

本机 SQL 运维命令通过 `python3 ops/postgresql-ha/with-sql-tls.py node <已有脚本>` 运行；已有 PGHOST/PGUSER 等仍由受保护环境提供，不能在命令行拼密码。包装器将三台已知 PG 私网地址转换为已有 s1/s2/s3 主机名，使 Node pg 显式校验 DNS servername。Ingestor 验证还应设置 CONSUMER_PGHOST 为当前主库节点名、CONSUMER_PGPORT=6432，避免使用历史 runtime.env 中的初始直连地址。跨机请复制 CA 到合适的受保护路径并配置对应客户端，不能用 disable/no-verify 临时凑合。

首次建库脚本不是当前集群的重复部署命令。`prepare-pgbouncer.py` 检测到 TLS 已接管时会拒绝覆盖，以免重写整份配置降级；日常用本批增量配置脚本。Temporal schema Job 已增加 TLS 和固定入口，但**本批不对已有库重新执行 setup-schema**。后续升级使用同样 TLS 设置的专用 update-schema Job。

回滚顺序：先确认现有主备仍健康；如已执行 enforce，先只移除 HBA 的 `SQL TLS managed: all` 托管段并 reload，同时把 PgBouncer client_tls_sslmode 改回 allow（保留 server verify-full），再回退对应客户端 Git 配置/connector 的 TLS 两字段。Filer 逐台恢复保存的原配置并重启确认。不要直接恢复整个历史 HBA 覆盖上一批复制/Grafana限制，不改 Patroni 候选/槽/offset，不重建数据库。

CA/服务器证书轮换与到期提醒沿用上一批待办。CA 轮换要先部署重叠信任，再换服务器证书；Node 进程需要滚动重建以加载启动时的额外 CA。连接池和 PostgreSQL共享节点身份时，更新证书后两者均需 reload。

核对的上游实现：SeaweedFS 4.47 `weed/filer/postgres/postgres_store.go`；Temporal v1.32.0 `common/persistence/sql/sqlplugin/postgresql/session/session.go`、`tools/sql/main.go`；本仓库锁定的 pg 8.23.0 `lib/connection-parameters.js`。
