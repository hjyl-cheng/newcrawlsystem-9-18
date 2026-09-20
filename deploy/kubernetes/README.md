# Kubernetes validation bootstrap

`kubeadm-validation.yaml` pins Kubernetes v1.31.14, containerd CRI, Calico VXLAN, and the three control-plane addresses.

The control endpoint is `https://k8s-api.crawl.internal:16443`. Every Kubernetes node must first install the node-local HAProxy and map this name to 127.0.0.1. HAProxy checks all three private API addresses on TCP 6443. Instructions, prerequisites, rollback boundaries, and node-join requirements are in `ops/kubernetes-ha/README.md`.

Existing nodes have been migrated in place. Do not rerun kubeadm init. This is not a public load balancer: operators can SSH into any available A node to administer the cluster. Calico VXLAN continues to use internal UDP 4789.
