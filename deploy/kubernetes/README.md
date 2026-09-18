# Kubernetes validation bootstrap

`kubeadm-validation.yaml` is the validation profile for the three A control-plane nodes.

The current internal endpoint is:

```text
k8s-api.crawl.internal:6443 -> 10.4.4.12
```

This endpoint is suitable for topology validation only. It is not the production HA endpoint while it resolves to A1. Before production, move the name to a verified internal load balancer or a tested cloud VIP, then renew/update the API server certificate SANs and validate control-plane failover.

The file pins Kubernetes `v1.31.14`, containerd CRI, the Calico pod CIDR, and the three control-plane addresses. Do not edit the cluster by hand without updating this file and the corresponding Argo/Git revision.
