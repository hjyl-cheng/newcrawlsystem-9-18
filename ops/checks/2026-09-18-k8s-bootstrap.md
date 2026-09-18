# Kubernetes 集群初始化记录

日期：2026-09-18

## 已完成

- A1 使用 `deploy/kubernetes/kubeadm-validation.yaml` 初始化 Kubernetes `v1.31.14`。
- A1 containerd CRI 正常，etcd、kube-apiserver、scheduler、controller-manager、CoreDNS 和 Calico 正常运行。
- 当前验证入口：`k8s-api.crawl.internal -> 10.4.4.12`。
- Calico v3.29.3 已部署到 A1。

## 已解决的网络阻断

A2/A3 最初无法访问 A1 的 TCP `6443`，因此 control-plane join 的 kubeadm 预检失败。云防火墙模板应用后，三台控制面已成功加入：

```text
A2/A3 -> 10.4.4.12:6443 = closed
A2/A3 -> 43.173.68.88:6443 = closed
```

A1 本机监听 `*:6443`，本机 API 健康，A1 的 UFW 未启用；原问题位于轻量云防火墙规则。

## 需要在云安全组放行的最小规则

源网段优先使用 `10.4.4.0/22`，不要对公网开放：

| 端口/协议 | 源 | 用途 |
|---|---|---|
| TCP 6443 | A1/A2/A3 内网 IP | Kubernetes API Server |
| TCP 2379-2380 | A1/A2/A3 内网 IP | 三控制面 etcd |
| TCP 10250 | A1/A2/A3 内网 IP | kubelet API |
| UDP 4789 | A1/A2/A3 内网 IP | Calico VXLAN（当前清单 `CALICO_IPV4POOL_VXLAN=Always`） |

## 当前验证结果

- A1/A2/A3 均为 `Ready`，Kubernetes `v1.31.14`。
- etcd、kube-apiserver、scheduler、controller-manager、CoreDNS 均正常。
- Calico v3.29.3 三个节点均正常，VXLAN 设备使用 UDP `4789`。
- 云防火墙模板已允许内网网段 `10.4.4.0/22` 的控制面端口。
- Argo CD v2.13.5 已迁移到 `argocd` 命名空间，全部 Pod Ready；`default` 中无 Argo CD Pod。

UDP `4789` 规则补齐后，三节点探针已完成双向 Pod、DNS、Service 实测，详见 `2026-09-18-infrastructure-gitops.md`。生产前仍需把 `k8s-api.crawl.internal` 从当前指向 A1 的验证入口，改为腾讯云内网负载均衡或经过验证的 VIP。
