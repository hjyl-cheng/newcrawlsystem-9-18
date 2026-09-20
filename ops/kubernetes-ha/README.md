# Kubernetes 管理入口与 Argo CD 可用性

本目录仅维护外围配置与验收。所有源文件在新仓库，业务开发继续暂停。

## 节点本地 API 入口

A1/A2/A3 各部署一个 systemd HAProxy 2.8.16（Ubuntu 安全更新包），监听 `127.0.0.1:16443`；TLS 透传至三个 API Server 的内网 6443，使用 Kubernetes CA 检查 `/readyz` 和证书身份。不挂载管理员私钥，不开放公网端口，不依赖 DNS 轮询或漂移 VIP。

每个控制节点的 `/etc/hosts` 将 `k8s-api.crawl.internal` 映射到本机 loopback。kubectl、kubelet、kube-proxy 使用 `https://k8s-api.crawl.internal:16443`。controller-manager/scheduler 仍连接各自节点的 API，靠原生 leader election 选择活动实例。集群内普通 Pod 使用原生 `kubernetes.default` Service；Calico CNI 继续使用 10.96.0.1，其网络启动所需 kube-proxy 已改用本地入口。

操作顺序：

1. `prepare-api.py a1/a2/a3` 分别安装并校验三个入口，不修改客户端。
2. `activate-api.py a2`、`a3`、`a1` 逐台备份配置、更新 kubeconfig/hosts、重启 kubelet并验证。旧配置保存在各节点 `/srv/crawlsystem/kubernetes-ha/pre-local-api`，0700。
3. `update-cluster-endpoint.py` 更新 kubeadm-config、cluster-info 和 kube-proxy 配置，滚动重建 kube-proxy。hostNetwork Pod 的 hosts 文件在创建时生成，因此需要滚动重建。
4. 更新仓库 kubeadm 配置。禁止对现有集群重新执行 kubeadm init。

新增 Kubernetes Worker 前必须先安装同类本地代理、可信集群 CA 和 hosts 映射，再用新控制端点执行 join。它只需访问 A1/A2/A3:6443；不必加入控制面/etcd。当前安装脚本的节点枚举仅列现有三个控制节点，增加节点时需补充明确清单，不能随意在 S 节点执行。

这解决节点和管理客户端对 A1 API 的单点依赖。A1 整机不可用时，运维人员通过 SSH 登录 A2/A3，那里已准备本机 kubectl 配置；这是三个可替代管理入口，**不是一个跨服务器自动漂移的公网地址**。公网统一地址需后续有实际域名/云 LB 资源才能配置。

## Argo CD

使用 v2.13.5 官方 HA manifest，SHA256 `61b2a4a383d80c29e2662f80e42fc63e27729f825c825f516f1993402816f65d`，原文保存在 `deploy/argocd/ha/vendor/install.yaml`。上游来源：`https://raw.githubusercontent.com/argoproj/argo-cd/v2.13.5/manifests/ha/install.yaml`。镜像使用已核对摘要；overlay 冻结小规模 CPU/内存预算。

- argocd-server、repo-server 各两副本，要求不同节点。
- Redis/Sentinel 三成员各占一个节点，Redis 代理两副本。它是 Argo 内部可重建缓存，没有新增采集业务数据库；持久权威状态仍在 Kubernetes etcd/Git。
- application-controller 保留官方稳定版单活 StatefulSet，优先调度 A2，但不硬绑定节点；PDB 防止维护时无意驱逐唯一控制器。
- Dex/ApplicationSet/Notifications 仍是单副本；当前未配置 SSO、ApplicationSet 或外部通知，本轮的本地登录和现有 Application 发布不依赖其并行副本。
- **单活 controller 在整机不可达时，StatefulSet 不会安全地自动强删旧 Pod。**必须先确认旧节点已停止/被隔离，再删除失联旧 Pod，才能调度替补。不能把 API/server/repo/Redis 的多副本写成整个 Argo 零人工接管。
- v2.13.5 的 Dynamic Cluster Distribution 官方标记 Alpha，当前不引入。若生产要求 controller 无人值守整机故障接管，需要另行采用经过验收的稳定接管机制或升级后评估官方能力。本次维护原版本以减少同时变更，安全升级仍需单独推进。

Argo 自身属于集群管理员引导层，由本仓库清单受控应用；不扩大业务 AppProject 的集群管理权限。应用部署仍由五个 Argo Application 自动同步唯一新仓库。

应用 HA 清单前先完成加密 etcd/config 备份。先单独应用 Redis HA 资源并等待三成员健康，再应用完整 overlay，最后确认客户端已经切到新 Redis 地址后删除旧单节点 Redis Deployment/Service。保留已有 `argocd-secret` 和 `argocd-redis` 认证 Secret，不重新生成登录凭据。

```sh
kubectl kustomize deploy/argocd/ha > /tmp/argocd-ha.yaml
kubectl apply --server-side --field-manager=crawl-argocd-ha --force-conflicts -f /tmp/argocd-ha.yaml
```

此命令接管官方引导资源的字段管理权；先审查差异，不能用于覆盖运行 Secret 数据。Argo CD 自身回退到旧清单会改变 Redis 地址与副本，需保留旧配置和等待恢复，不直接盲目删除 HA 缓存成员。
