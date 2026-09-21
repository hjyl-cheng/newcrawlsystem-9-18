# PostgreSQL SQL 通道分阶段加密

2026-09-21。本批覆盖三 S 节点的 PG 服务端证书、Patroni 物理复制/槽位同步和 rewind 客户端、Grafana 数据库连接。端口仍为 5432，固定 HAProxy 入口透传 TLS；不新增数据库、服务器或 SQLite，不改 CDC guard 的晋升名单、槽和 WAL 屏障。

## 调用关系与批次边界

| 调用方 → 接收方 | 当前方案 / 后续工作 |
|---|---|
| Patroni / etcd / HAProxy 健康检查 | 原有独立 mTLS、DCS 权限，保持 |
| S 备库 → PG 主库 5432 | 本批改为 verify-full；replicator 的 SQL 与 replication 协议均拒绝明文 |
| Patroni rewind → PG 主库 5432 | 本批配置 verify-full、限制明文；验证账号连接，不把受控切主当成真实分叉后的 rewind 验收 |
| Grafana → postgres-rw:5432 → PG | 本批 verify-full + 专用角色/SCRAM；三台 PG 拒绝该角色明文 |
| Temporal / Debezium → postgres-rw:5432 | 下一批迁移；仍允许其现有连接方式 |
| Ingestor → postgres-rw:6432 → PgBouncer → PG | 下一批同时处理客户端和连接池后端，不能只加密一段 |
| Seaweed Filer → 本机 15432 → 主机 PgBouncer:6432 → PG | 下一批；本机 HAProxy 保留，需匹配 TLS 名称 |
| pgBackRest / 本机探针 → PG | 本机 Unix socket；跨机备份使用原 SSH 通道 |
| Producer / Consumer / Connect / exporter → Kafka:9092 | 仍 PLAINTEXT；后续先兼容 TLS、迁移全部调用方、再关旧通道；9093 已用于 KRaft controller |
| Seaweed 内部 HTTP/gRPC | 仍待单独迁移；S3 签名认证不等于全部内部接口加密 |
| 六机 Vector / Grafana → 日志 ClickHouse:8443 | 已有 HTTPS 与专用受限账号，保持 |

## 部署顺序

1. 检查三成员健康、同步备库存在。执行 `python3 ops/postgresql-ha/secure-sql-transport.py servers --execute`。专用 CA/密钥仅放忽略的 `secrets/postgresql-ha/sql-pki/`；各 S 节点只获得自己的服务端密钥与 CA，目录 `/etc/crawl-patroni/sql-pki`，postgres/0700、文件0600。CA 私钥不下发。脚本保存原配置到忽略目录，仅 reload，不 restart PG。
2. 证书包含节点私网 IP/节点名和固定 Service 的 DNS 名称；有效期一年，CA 五年。脚本检查每台的 IP 和固定 DNS 验证，生成 Grafana 的 `postgresql-sql-ca` Secret。此时旧客户端保持兼容。
3. 执行 `... replication --execute`。逐备库更新 Patroni `authentication.replication` / `rewind` 的 sslmode/sslrootcert，等待真实 WAL receiver 重新连接，再更新主库以备下次降级。确认两个真实 walsender 均使用 TLS 后，在 HBA 最前插入相应角色的 `hostnossl ... reject`，防止下面的宽泛内网规则放行明文。
4. 提交/推送 `ops/monitoring/render.py` 和生成清单，由 Argo 将两个 Grafana Pod 滚动更新到 verify-full，挂载 CA。一次最多一个不可用。`prepare-cluster.py` 同步保留 hostssl 和 CA 配置，防止重建时降级。
5. 两 Pod 都 Ready 且 PG 内真实连接全部使用 TLS 后，执行 `... enforce-grafana --execute`，再拒绝 Grafana 明文连接。
6. 执行 `python3 ops/postgresql-ha/verify-sql-tls.py --execute`：错误 CA/错误名称/明文拒绝、真实角色与固定入口、Grafana/日志查询、受控切主及切回、复制/CDC 小数据确认。不做压力测试；不停止两台 PG；不删业务槽。验收记录单独保存。
7. 配置与密钥沿用 control 加密备份到 A1/S2（已有 `/etc/crawl-patroni` 和 operator secrets 备份范围），PG 差异备份随本轮收尾检查。

## 回滚与维护

每阶段可停在兼容状态。禁止直接恢复整个旧 Patroni 配置覆盖后续 CDC/tag 修改，也禁止关闭 Patroni 手工启库。

- 若 Grafana 客户端迁移失败，尚未执行 enforce 阶段时可以回退本次 Grafana Git 提交；如已收紧 HBA，先只移除 `# SQL TLS managed: crawl_grafana` 到同名 `END` 的托管段并 reload，再回退。恢复期间仍保留原账号/来源限制。
- 若复制验证失败，先只移除复制/rewind 的托管 HBA 段并 reload；再逐台将 Patroni 的 replication/rewind 的 `sslmode`/`sslrootcert` 两字段恢复为 `*-before.patroni.json` 中的值（原不存在则删除），reload 并等待两备 streaming；不改其他字段。
- 服务端证书需要回退时，先确认所有 verify-full 客户端已回退，再按保存值还原四个 SSL 参数（原不存在则删除本次添加值），reload；不要恢复/删除数据目录。
- 同 CA 换证：检查 SAN 和公私钥配对、原子更换节点证书/密钥、reload、逐台握手验证。CA 轮换必须先让客户端信任新旧 CA，再换服务端，再去掉旧 CA；目前尚未实现自动轮换/到期告警。
- 服务器证书只证明服务器身份；客户端继续用专用账号和 SCRAM 认证。本批没有要求全部 SQL 客户端证书认证，也没有声称所有 PG 流量已经强制 TLS。

旧首次接管脚本 `prepare-patroni.py` 不是可随意重跑的配置管理器。新增/恢复 PG 节点须先完成既有恢复流程，再安装 SQL PKI、应用本文件的客户端和 HBA 配置，验证后才准入。
