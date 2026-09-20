# Kubernetes 管理入口与 Argo CD 可用性验收

日期：2026-09-20。范围为外围基础设施，不新增采集业务代码；唯一新仓库，旧仓库未修改。

## 已实际部署

| 内容 | 最终位置/配置 |
|---|---|
| Kubernetes API 本地入口 | A1/A2/A3 各一个 systemd HAProxy；127.0.0.1:16443 → 三个私网 API:6443 |
| Kubernetes 客户端 | 三节点 kubelet、管理员 kubeconfig、kube-proxy 使用 k8s-api.crawl.internal:16443；节点 hosts 指向 loopback |
| 集群发现信息 | kubeadm-config、cluster-info、仓库 kubeadm 配置同步更新 |
| CoreDNS | 两副本从同在 A1 改为 A2/A3；强制跨节点、滚动时保留旧实例、PDB minAvailable=1 |
| Argo 页面/API服务 | 两副本，A2/A3 |
| Argo repo-server | 两副本，A2/A3 |
| Argo 内部 Redis/Sentinel | 三个成员，分别 A1/A2/A3；Redis HAProxy 两副本，A1/A3；旧单 Redis Deployment/Service 已移除 |
| Argo application-controller | 官方稳定配置的单活 StatefulSet；受控迁移后位于 A3，软调度偏好仍为 A2 |
| Argo 辅助服务 | Dex/ApplicationSet/Notifications 仍单副本；目前没有配置 SSO、ApplicationSet 或外部通知 |

API 入口采用 Ubuntu HAProxy 2.8.16 安全更新包。TLS 原样透传，后端 /readyz 使用集群 CA 验证身份；健康周期 2s、连续两次成功/失败，失效后关闭旧连接促使重连。不新增云防火墙端口、不使用漂移 VIP、不关闭 API 认证。原 A 节点管理员证书的权限不变，配置权限保持受保护。

Argo 使用 v2.13.5 官方 HA manifest，vendor SHA256 `61b2a4a383d80c29e2662f80e42fc63e27729f825c825f516f1993402816f65d`。所有镜像在 overlay 固定摘要，设置验证期 CPU/内存预算；不是对生产负载容量的结论。Redis 是可重建的 Argo 缓存，权威配置仍在 Kubernetes etcd/Git；没有新增采集侧数据库。

Argo 自身由管理员受控应用仓库 overlay，应用仍由五个 Argo Application 自动同步新仓库；没有扩大业务 AppProject 的集群权限。原 argocd-secret、argocd-redis 认证数据前后 SHA256 一致，没有重设登录密码。

## 验证结果

1. **A1 API 真正停止后的管理访问**：将 API static Pod manifest 临时移出监听目录，确认 A1:6443 不再监听，再通过三节点本地入口分别读取 readyz/三节点列表；A2 写入临时 ConfigMap，A3 成功读回。整个过程有定时恢复与 finally 恢复，最终 A1 API 正常。
2. **A1 API 停止期间的实际 Git 发布**：发布提交 `69d073f483227e1f6ec615cc4bdc60ef988f3356`；Argo 拉取新仓库，三节点 infra-smoke 更新到 `crawlsystem-infra-validation-ha-20260920-v2`，达到 Synced/Healthy；期间完成三节点相互 Pod HTTP、DNS、Service 及 S 节点存储 TCP 检查。发布约 110.52 秒，主要包含 DaemonSet 三次默认 30 秒退出等待；该数字不是 API 切换 RTO。初次 60 秒验证窗口过短，已按实际滚动时间调整。
3. **DNS 分散**：最终两 DNS Pod 在 A2/A3；随后重新运行网络检查通过。CoreDNS 补丁需在 kubeadm addon 升级后复核。
4. **Redis 主成员正常替换**：替换 redis-ha-server-1，包含官方 preStop 切换；约 11.73 秒观察到 server-0 接任主角色，原主离线时剩余两 Sentinel 达到多数，认证 PING 经固定缓存入口恢复，随后三成员重新 Ready。是正常替换，不是突然断电测试。首次检查从 Redis Pod 访问代理被官方 NetworkPolicy 拒绝；改用临时受信客户端，从正确的应用网络身份验证，未放宽策略。
5. **页面/API HTTPS**：使用原服务证书验证域名和信任，固定 Service 返回 v2.13.5+03600ae；A2、A3 两个 Pod 的 `/healthz?full=true` 都返回 ok。没有以跳过证书验证代替该检查。
6. **发布控制器受控跨节点恢复**：临时 cordon A1/A2，正常删除并等待 A2 旧控制器 Pod 停止，在 A3 建立新的 UID，11.71 秒 Ready；随后改动 infra-smoke 的 ConfigMap，由新控制器从 Git 自动修复。节点已全部 uncordon，测试探针和临时配置已清理。
7. **当前业务验证服务**：Data Ingestor、Temporal、PG 固定入口、runtime-smoke 均 2/2 Ready；五个 Argo Application Synced/Healthy。

JSON 证据：`2026-09-20-kubernetes-api-failover.json`、`2026-09-20-argocd-cache-failover.json`、`2026-09-20-argocd-controller-recovery.json`。临时 Redis 客户端始终 NotReady，未被页面 Service 当作用户流量后端，已删除。

## 必须保留的边界

- **未完成整个 Argo 的无人值守整机故障接管**。稳定版发布控制器仍单活；节点失联时 StatefulSet 不能贸然强删旧 Pod，需要先确认旧节点关机或隔离，然后接管。受控删除测试不能代替失联节点 fencing。
- 官方 v2.13.5 Dynamic Cluster Distribution 仍标 Alpha，本轮未引入。后续若要求控制器所在机器突然失联也全自动接管，需单独确定稳定实现/版本及隔离能力并验证。
- API 停机演练保留 A1 OS、etcd、kubelet 和工作负载；没有把它写成 A1 整机断电实测。管理员在 A1 整机不可用时可 SSH A2/A3 管理；本轮未创建统一公网负载均衡地址。
- Argo/Dex/Redis 继续使用已有产品版本；固定摘要不等于已经完成安全版本升级。版本升级、TLS/认证细化、监控告警和证书到期告警另行推进。Sentinel 控制接口依赖命名空间 NetworkPolicy 隔离，不能把 Redis 数据认证误当成 Sentinel 全接口认证。
- Argo 缓存可丢失重建；没有宣称其 emptyDir 是持久存储。控制面定时备份仍从 A1 发起，其调度容灾与异地备份不是本轮完成项。
- 当前改动及脚本都在新仓库；API 原配置在各 A 节点 `/srv/crawlsystem/kubernetes-ha/pre-local-api`。控制备份已包含 `/etc/crawl-kube-api`，Argo 和 DNS 对象由 etcd 快照覆盖。

按 24.12 继续外围工作：本批完成 API 入口、DNS 分散、Argo server/repo/cache 多副本及受控恢复；发布控制器无人值守接管保留明确待办。随后处理 Kafka/SeaweedFS/ClickHouse/CDC 的可靠性，业务开发仍暂停。


最新备份：`control-20260920T110653Z.tar.gpg` 已加密保存到 A1/S2，包含新 API 入口配置、Argo HA 与 DNS 对象；源版本 `966da4f`。独立恢复通过：9 份文件校验一致，恢复 Kubernetes registry 1100 / Secrets 11 / Deployments 12、PG 协调键 10，PG 协调认证保持开启。三节点本地入口最终均 active，域名均解析到本机 127.0.0.1，readyz 均返回 ok。
