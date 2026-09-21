# 2026-09-21 集中指标与平台内告警验收

范围：24.12 第 5 项的指标/平台内告警切片。没有恢复业务功能开发，没有引入 SQLite 或调用 TypeSafe API。仍不等于集中日志、安全收尾、外围综合验收全部完成。

## 现场部署

| 位置 | 内容 |
|---|---|
| A1/A2/A3/S1/S2/S3 | 私网 mTLS node_exporter、30 秒只读 textfile 探针 |
| A2/A3 | 两份 Prometheus，本地 PV，各保留 3 天/3GB |
| A1/A2/A3 | 三份 Alertmanager，本地 PV、私有 gossip 集群 |
| A1/A2 | Grafana、KSM、Kafka exporter、HTTP 探针各两份（调度可变，强制同组件跨节点） |
| 现有 S 集群 | 新增 `crawler_grafana` 逻辑库与专用角色，复用现有 PG 主备/备份 |

监控命名空间 `crawl-monitoring`；独立受限 Argo Project/Application；当前 13 个监控 Pod，全部 Ready。集群级 PV/StorageClass/只读 RBAC 由运维引导脚本维护，不放宽原 crawl-validation 项目的资源权限。镜像 digest 固定，未使用 latest。

用户已放通六机 TCP 9100 / `10.4.4.0/22`；六个 mTLS 请求返回 200，全部本地 collector 成功。无客户端证书请求被拒绝。Calico 主机跨节点访问实际使用 VXLAN 源地址，Grafana NetworkPolicy 增加三个确切 /32；未放开整个 Pod 网段。

## 验证与证据

最终执行结果 **PASSED**：告警触发 78.44 秒，恢复解除 30.08 秒（采样窗口下的观测值，不是生产 SLO）。

机器可读执行结果见 `2026-09-21-monitoring-evidence.json`；可重复脚本 `ops/monitoring/verify.py --execute`。

- 官方 promtool 校验：34 条规则通过；4 组规则测试覆盖连续离线/恢复、探针失败、无效/临时 CDC 槽、维护备份待办不被误当正常定时备份。Kubernetes server dry-run 通过。
- 两份 Prometheus 各 14/14 targets 正常：六主机、两 Prometheus、三 Alertmanager、KSM、Kafka、HTTP 探针。三 A 节点 Ready，PG 主库/副本与槽位/CDC guard 指标正常。
- Alertmanager API 确认三成员 ready；受控停止 A3 的 node_exporter 后，两 Prometheus 触发同一节点告警，Alertmanager 仅一条且无 replica 标签；恢复后解除。演练前安装 4 分钟自动救援定时器，finally 恢复并移除定时器。未停止数据库/队列/业务探针服务。
- Grafana `/api/health` 多次为 database ok；匿名查询 401；凭据认证成功；管理 API 确认 `postgres/crawler_grafana`，面板 16 项；通过 Grafana 代理查询 Prometheus 与 Alertmanager 均正常。
- 删除并重建 `prometheus-0`，绑定仍是 `crawl-prometheus-a2`；重建前历史时间点样本重建后可查询，另一份继续采集。这是 Pod 重建演练，不是整机断电或双机丢盘演练。
- 首轮终验曾在 Prometheus 重启后从即时 `ALERTS` 查询读到残留 CDC 告警；核对实际 guard 为 READY、规则 API 为 inactive，随后查询消失。终验脚本增加等待：规则引擎与历史查询窗口均稳定后再接受结果，不能仅以 Pod Ready 推断告警状态。
- 终验只保留真实已知待办 `SeaweedContinuousBackupPending`：目前对象备份仅支持维护基线，未搭建持续对象备份。没有人为隐藏这条告警。

节点资源抽查（验证负载，非压力测试）：A1/A2 可用内存约 4.9/5.4GiB，S 节点约 5.2–5.9GiB，系统盘仍有余量。保留现有六机规格即可继续当前外围验证；不据此承诺生产吞吐。

## 入口与备份

Grafana Service：`10.107.82.103:3000`，仅集群内网。自己电脑执行：

```sh
ssh -N -L 3000:10.107.82.103:3000 ubuntu@43.173.68.88
```

随后打开 `http://localhost:3000`，用户名 `admin`，初始密码采用用户指定值（不入 Git）。面板位于“基础设施 / 爬虫平台 · 基础设施总览”。隧道窗口需保持运行；Service 重建后核对地址。

Grafana 建库/初始化后的 PG 差异备份：`20260920-172412F_20260921-115314D`，仓库存储增量 3,445,440 字节。监控配置/证书目录加入控制面加密备份范围；operator Secrets 和 Kubernetes Secrets 也沿用现有加密备份。Prometheus 本地短期历史不承诺灾备恢复。

本轮新增控制面加密备份：`control-20260921T035926Z.tar.gpg`，A1/S2 双份，源提交 `86cb22c`。已解密检查 manifest 中全部文件 SHA256，并确认六台节点的监控服务配置、TLS 私钥及采集脚本均被包含；未重复宣称完整集群重建演练。

## 仍待完成

- 集中日志尚未安装；当前日志仍通过 journald/kubectl 查看。
- 只有平台内告警，没有配置或发送短信/微信/邮件等外部通知；整套集群不可访问时不能依赖这套内置面板主动通知人。
- Kafka 认证/ACL/TLS、PG 传输加密、Seaweed 内部认证与证书自动轮换等安全收尾未完成。监控 HTTP 探针只执行 GET，但 Connect REST 本身无只读角色，需限制监控 namespace 的写权限。
- Argo 发布控制器的整机失联无人值守接管、Seaweed 持续备份、ClickHouse 单机/系统盘、异地灾备、CDC 更广故障矩阵等原有边界不变。
- Prometheus 两份是独立采集/独立历史；无 Thanos/远端存储合并。Grafana 多副本共用 PG，不消除其 PG 依赖；现有入口也不是公网高可用入口。

后续继续第 5 项的集中日志与凭据边界，再做外围综合验收；业务开发继续暂停。
