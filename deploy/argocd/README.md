# Argo CD 验证部署

Argo CD 固定部署到 `argocd` 命名空间。官方安装清单本身不创建命名空间，因此必须先执行：

```bash
kubectl apply -f deploy/argocd/namespace.yaml
kubectl apply -n argocd -f deploy/argocd/argo-cd-v2.13.5-install.yaml
```

检查：

```bash
kubectl get pods -n argocd
kubectl get pods -n default -l app.kubernetes.io/part-of=argocd
```

第二条命令应当没有资源。生产环境再将 Argo CD 的副本、持久化、入口和 Secret 纳入对应 overlay。

## 新仓库 GitOps 验证

唯一来源：`https://github.com/hjyl-cheng/newcrawlsystem-9-18.git`，分支 `main`。
当前仓库为公开仓库，Argo CD 通过 HTTPS 匿名只读拉取，不注入 GitHub 个人私钥。
若以后转为私有，需为该仓库单独配置只读 Deploy Key 或 GitHub App 凭据。

首次引导顺序（清单需先提交并推送）：

```bash
kubectl apply -f deploy/argocd/bootstrap/namespace.yaml
kubectl apply -f deploy/argocd/bootstrap/project.yaml
kubectl apply -f deploy/argocd/bootstrap/infra-smoke.yaml
```

AppProject 只允许新仓库向 `crawl-validation` 部署 ConfigMap、Service、DaemonSet，
禁止管理集群级资源。Application 自动同步、自动修复漂移并清理旧资源。
引导清单由管理机应用；探针工作负载完全由 Argo CD 从 Git 创建。

目录：

- `deploy/base/infra-smoke`：固定镜像 digest 的 BusyBox HTTP 基础设施探针。
- `deploy/overlays/validation/infra-smoke`：验证环境 Kustomize overlay。
- A1/A2/A3 各运行一个 Pod，提供 ClusterIP Service，无公网入口。
- 该探针用于网络、发布和回滚验收，不含业务代码，也不表示已经建立业务镜像 CI。

检查状态：

```bash
kubectl get application crawl-infra-validation -n argocd
kubectl get pods -n crawl-validation -o wide
python3 ops/scripts/check-cluster-network.py
```

发布：修改 Git 中的探针内容或清单，渲染检查后 commit/push。
ConfigMap 名称包含内容哈希，变化会触发 DaemonSet 滚动更新。
需要立即检查时可刷新仓库缓存：

```bash
kubectl annotate application crawl-infra-validation -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

回滚：对要撤销的发布提交执行 `git revert <commit>` 后 push。
Argo CD 同步这个新的回滚提交。不要直接修改线上 Pod 或执行 `kubectl rollout undo`，
否则自动同步会按 Git 状态覆盖它。

`Synced / Healthy` 表示声明已同步且 Kubernetes 就绪检查通过；
跨节点 Pod、DNS、Service 和存储访问仍需独立实测。
