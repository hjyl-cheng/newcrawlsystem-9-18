# Kubernetes 集群初始化记录

日期：2026-09-18

## 已完成

- A1 使用 `deploy/kubernetes/kubeadm-validation.yaml` 初始化 Kubernetes `v1.31.14`。
- A1 containerd CRI 正常，etcd、kube-apiserver、scheduler、controller-manager、CoreDNS 和 Calico 正常运行。
- 当前验证入口：`k8s-api.crawl.internal -> 10.4.4.12`。
- Calico v3.29.3 已部署到 A1。

## 当前阻断

A2/A3 无法访问 A1 的 TCP `6443`，因此 control-plane join 的 kubeadm 预检失败：

```text
A2/A3 -> 10.4.4.12:6443 = closed
A2/A3 -> 43.173.68.88:6443 = closed
```

A1 本机监听 `*:6443`，本机 API 健康，A1 的 UFW 未启用；问题位于云安全组/网络访问控制。

## 需要在云安全组放行的最小规则

源网段优先使用 `10.4.4.0/22`，不要对公网开放：

| 端口/协议 | 源 | 用途 |
|---|---|---|
| TCP 6443 | A1/A2/A3 内网 IP | Kubernetes API Server |
| TCP 2379-2380 | A1/A2/A3 内网 IP | 三控制面 etcd |
| TCP 10250 | A1/A2/A3 内网 IP | kubelet API |
| TCP 179 | A1/A2/A3 内网 IP | Calico BGP（当前清单使用 bird） |
| IP protocol 4 | A1/A2/A3 内网 IP | Calico IP-in-IP（当前清单 `CALICO_IPV4POOL_IPIP=Always`） |

放行后重新验证 `10.4.4.12:6443`，再继续 A2/A3 加入；没有放行前不重复执行 kubeadm join。
