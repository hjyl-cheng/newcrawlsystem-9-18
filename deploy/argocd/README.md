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
