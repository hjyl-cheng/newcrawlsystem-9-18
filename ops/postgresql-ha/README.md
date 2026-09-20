# PostgreSQL 主备管理与固定入口

现有 PG 17 数据目录原地接管，数据库不分片。S1/S2/S3 各运行 PostgreSQL、Patroni、独立 PG etcd 成员和 PgBouncer。PG etcd 与 A 节点的 Kubernetes etcd 完全分离。

Kubernetes 中两个跨节点 HAProxy Pod 提供 `postgres-rw.crawl-validation.svc:5432`（直连当前主库）和 `:6432`（当前主库的事务连接池）。Temporal 使用 5432 保留会话语义；Data Ingestor 使用 6432。每 2 秒以独立 CA 的 mTLS 查询 Patroni `/primary`，失去主身份时关闭旧连接，由应用重连。Service 没有公网入口，不使用漂移 VIP。

## 部署与接管

1. `bootstrap-coordination.py`：安装哈希固定的 etcd 3.5.24、专用 TLS CA；三成员启动。
2. `secure-coordination.py`：启用认证。Patroni 仅访问 `/service/crawl-pg/`；管理员证书独立保管。HTTP gateway 客户端证书必须没有 CN，同时用用户名/密码认证；etcdctl 管理员使用 gRPC 证书 CN 认证。
3. `prepare-backup-ha.py`：扩展已有加密 pgBackRest 仓库，识别三台 PG，并实际执行 check 与差异备份。S2 的仓库配置和本机 PG 配置分开，备份仍存 S2。
4. `prepare-patroni.py`：生成受保护配置、独立 REST CA、受限 rewind 账号、watchdog 设备权限；备份原配置到 `/srv/crawlsystem/pg-ha/pre-patroni/main`。不启动 Patroni，不初始化数据库。
5. 确認新备份成功、现有主备一致后，依次 `adopt-node.py s1`、`s2`、`s3`。会产生短暂重连；严格同步在备库接入前可能等待。原 `postgresql@17-main` 被 mask，今后由 `crawl-patroni` 唯一管理。不要对同一数据目录手工启动 postgres。
6. `prepare-pgbouncer.py`：三节点准备相同映射与事务连接池；凭据只来自本地忽略目录，终端不输出密码。
7. 用 `secrets/postgresql-ha/rest-pki/ca.crt` 和合并的 `client.pem` 创建 `patroni-health-client` Secret；提交 Git 后应用 `deploy/argocd/bootstrap/postgresql-entry.yaml`。验证端口，再以 Git 修改应用连接地址。

这些脚本是本集群受控部署工具，不是随意反复运行的通用安装器。接管脚本必须按当时实际主身份执行；已接管集群不要再次运行首次准备/接管脚本。禁止自动 initdb、克隆覆盖数据目录；rewind 失败必须人工检查后恢复。

## 数据与防双主边界

- `synchronous_mode=true`、`synchronous_mode_strict=true`、`synchronous_node_count=1`、`synchronous_commit=on`。正常一主、一同步备库、一额外备库；同步确认是 WAL 落盘，不代表所有备库已应用。无可用同步备库时写入等待，不能宣称任意两台故障仍可写。
- `wal_log_hints=on` 支持 pg_rewind；保留原数据目录，不配置自动清空重建。复制槽有 2 GB WAL 保留上限，长期离线备库可能需受控重建。
- Patroni 租约 TTL 30s、loop_wait 5s、retry_timeout 5s。softdog 必须可用才允许成为主库，超时 20s 早于租约过期。它是 Linux 软件看门狗，不能保证覆盖宿主机/内核彻底冻结等所有失效；不等同云侧硬件 fencing。
- Patroni REST 强制 mTLS；修改操作另需账号密码。HAProxy 仅有健康证书，没有 REST 管理密码或 DCS 管理凭据。
- 检测与重连有间隔，不承诺零中断；已提交响应丢失仍需上层幂等处理。实测 RTO/RPO 以 `ops/checks` 为准。
- 后续仍需数据库传输加密细化、监控告警、证书到期告警、异地备份；不要把这一项等同全部外围完成。

## 运维命令

通过 SSH 在任一 S 节点执行：

```sh
sudo -u postgres patronictl -c /etc/crawl-patroni/patroni.yml list
sudo -u postgres patronictl -c /etc/crawl-patroni/patroni.yml switchover --leader s1 --candidate s2 --force
```

先检查真实 leader/sync 状态再选择目标。禁止仅因入口故障就手动 promote。DCS 无多数派时优先恢复多数派，不删除 leader key 强行双主。

S2 备份命令仍为 `sudo -u pgbackrest pgbackrest --stanza=crawler check` / `--type=diff backup`；各 PG 节点归档使用 `--config=/etc/pgbackrest/pg-node.conf`。原库单机回滚配置仅在确认所有 Patroni 已停止、唯一主库身份清楚且旧主已隔离后进行，不能直接 unmask 启动第二个写库。

## PgBouncer 驱动兼容

当前验证组合为 PG 17.11、PgBouncer 1.25.2、Node pg（以 package-lock.json 为准），显式 `max_prepared_statements=100`。协议级 named prepared statement、事务级 advisory lock 和现有入库幂等回执会经过实际连接池验证。禁止新增依赖 session advisory lock、SQL-level PREPARE、跨事务临时状态的事务池业务。

Node pg 的 `statement_timeout` 与 `idle_in_transaction_session_timeout` 启动参数不能直接由此连接池转发。已在 `crawler_ingestor_validation`/`crawler_validation_ingestor` 的角色库默认值设置 30s/45s，池忽略同名启动参数；实际 `SHOW` 验证仍为 30s/45s。以后修改应用超时需同步修改角色库默认值，不应仅修改客户端配置。

## 与 24.4 的实施对应

保持一个逻辑采集 PG，不分片；同步模式、稳定入口、事务池兼容均按现场冻结。采用独立 systemd Patroni/etcd 与集群内 HAProxy，不新装整套 Pigsty，也不假设云网络支持漂移 VIP。Temporal 保留会话语义，使用 5432 直连通道。**24.4 要求的 Debezium logical slot failover 尚未实施**：当前 wal_level=replica、尚无 CDC Connector，不能把本次物理 PG HA 写成 CDC/Publication 已 HA。该项需随外围 Kafka Connect/Debezium 建设补齐并单独演练。
