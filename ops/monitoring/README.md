# 基础设施监控与平台内告警

范围：外围监控，不含业务开发。Prometheus 规则直接判断实际指标，没有引入 TypeSafe API 或 SQLite。集中日志已按 `ops/logging/README.md` 接入；外部通知尚未接入。

## 部署与数据位置

- 六台 A/S 主机：systemd node_exporter 1.12.1，私网 TCP 9100，仅接受专用 CA 的客户端证书。每 30 秒运行只读本地探针，暴露系统服务、PG/槽位、CDC guard、存储与备份摘要，不输出凭据、日志或业务标识。
- A2/A3：Prometheus 各一份，独立本地 TSDB；保留 3 天或 3GB，5Gi 本地 PV。PV 是目录容量声明，不是磁盘配额。机器故障时另一份继续采集，原节点历史不会自动迁移；没有跨副本历史查询合并。
- A1/A2/A3：Alertmanager 各一份，本地 1Gi PV，TCP/UDP 9094 私有 Pod 网络组网。两份 Prometheus 删除 replica 告警标签后投递全部三成员，减少重复告警。只展示平台内告警，没有短信、邮件、Webhook 接收器。
- Grafana 两副本：复用现有 PG HA 的独立逻辑数据库 `crawler_grafana`，专用最小权限角色；通过稳定入口 5432 连接，避开事务池的迁移锁问题。面板/数据源由 Git 配置；SQLite 未启用。数据库连接已使用 verify-full TLS，校验专用 CA 与稳定入口名称；三台 PG 均拒绝 Grafana 角色明文连接。
- kube-state-metrics、Kafka exporter、只读 HTTP 探针各两副本。副本互斥分散到 A 节点。所有容器有资源上限，镜像 tag/digest 固定在 `images.json`。
- JSON Exporter 0.8.0 与 Blackbox Exporter 0.28.0 各两副本，只读对照 Connect 状态和 Ingestor 健康接口。它们尚未接替 `infra-exporter` 的告警。JSON Exporter 0.8.0 统计数组中的任务对象，不能把响应里的状态字符串直接映射成数值。
- postgres_exporter 0.20.1 两副本，只读对照主库复制、槽位、活动和设置。专用角色 `crawl_pg_exporter` 只有 `pg_monitor`，经稳定入口 verify-full 连接 `postgres`；来源限定三台应用节点。它不采集业务库和 WAL 目录，也不接替 `crawl_pg_*` 告警。角色和 HBA 由 `prepare-pg-exporter.py` 建立，密码留在 `secrets/monitoring/pg-exporter-password`。

配置源：`render.py` → `deploy/base/monitoring` → validation overlay → 独立 Argo Project/Application。监控应用仅管理 `crawl-monitoring`，不能修改集群级 RBAC/PV。`cluster-resources.yaml` 的本地 PV/StorageClass/只读 KSM RBAC 由运维显式引导。Secrets 不入 Git。

## 重建步骤

1. 根据 `node-exporter-release.json` 下载并校验官方二进制，保存 `/tmp/crawl-node-exporter`；恢复加密备份内 `secrets/monitoring`（不要重新生成 CA 破坏现有信任）。运行 `python3 ops/monitoring/bootstrap-nodes.py --execute`。
2. 六台轻量云规则：TCP 9100，来源 `10.4.4.0/22`。无需向公网开放指标/数据库/告警端口。
3. 先恢复/配置 PG SQL PKI 并完成 `ops/postgresql-ha/SQL-TLS.md` 的 servers 阶段；在保护文件 `secrets/monitoring/grafana-admin-password` 提供后台初始密码；运行 `python3 ops/monitoring/render.py`、`python3 ops/monitoring/prepare-cluster.py --execute`。初始密码仅影响首次建立管理员，后续改密码使用 Grafana 管理界面。
4. `promtool check rules deploy/base/monitoring/rules.yaml` 和 `promtool test rules ops/monitoring/rules-test.yaml`；`kubectl kustomize deploy/overlays/validation/monitoring` 检查最终清单。
5. 推送唯一新仓库后，apply `deploy/argocd/bootstrap/monitoring-project.yaml` 与 `monitoring.yaml`；等待全部 Pod Ready、Argo Synced/Healthy。
6. 检查两份 Prometheus 全部 targets、Grafana PG 健康、三个 Alertmanager 成员；按验收记录做有限故障演练。

node_exporter 的独立 TLS 证书有效期 2 年（CA 5 年），到期前需续签并滚动重启；当前尚无自动轮换。私钥通过 Secret/受保护文件下发。监控节点配置与 operator Secrets 纳入控制面加密备份，Grafana 数据纳入 PG 物理备份；TSDB 不作灾备承诺。

## 访问与处理

Grafana 保持 ClusterIP 内网入口（当前 `10.107.82.103:3000`）。在本地电脑建立 SSH 隧道：

```sh
# 先在 A1 查地址
kubectl -n crawl-monitoring get svc grafana
# 再在自己电脑执行（Service 重建后先核对地址）；保持 SSH 窗口开启
ssh -N -L 3000:10.107.82.103:3000 ubuntu@43.173.68.88
```

浏览器 `http://localhost:3000`，用户 `admin`；初始密码使用用户指定值，明文不写入本文。进入“基础设施 / 爬虫平台 · 基础设施总览”；Alerting 页面可选择外部 Alertmanager 查看活动告警。面板中 `ALERTS` 同样显示告警。

- TargetDown：先查私网连通与 node_exporter 服务，再查证书。不要把“采集不到”当成业务一定停机。
- LocalProbeFailed/Stale：查 `journalctl -u crawl-metrics-collect`，该指标特意区分探针失败和正常值。
- PG/CDC：结合 `ops/cdc/HA-GUARD.md` 检查 Patroni、复制/槽位、guard 状态与候选；不能直接删除活动 CDC 槽或跳过 LSN。
- Kafka：检查三个 broker、分区 ISR 与消费者；持续 lag 只表示积压，阈值需随生产负载调整。
- Backup：查看对应定时器/service、受保护 last-success 元数据和副本。`SeaweedContinuousBackupPending` 是真实已知待办，持续显示，不能以“告警清零”隐藏持续备份尚未建设的问题。
- 节点资源：磁盘 <15%、可用内存 <10%、CPU >90% 为起步阈值，本轮不代表压力测试/SLO 定案。

HTTP 探针仅 GET Connect 精确状态与 Ingestor readiness；Connect REST 本身没有只读角色，NetworkPolicy 只能限制来源/端口，需保持 monitoring 命名空间写权限受控。Prometheus/Alertmanager 自身没有公网入口或用户认证；通过命名空间 NetworkPolicy 隔离。Kafka 仍是现有 PLAINTEXT 边界，正式认证/TLS 另行收尾。

Grafana NetworkPolicy 还显式允许三 A 节点的 Calico VXLAN 地址：A1 `192.168.141.132`、A2 `192.168.78.192`、A3 `192.168.65.64`。主机经隧道访问远端 Pod 时可能使用此源地址；没有向整个 Pod 网段放行。重建节点或更换 Calico 地址后，应从 Node 注解重新核对并更新 `render.py` 的 TUNNELS。
