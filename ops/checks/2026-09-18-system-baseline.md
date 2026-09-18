# 系统基线检查

检查日期：2026-09-18  
执行节点：A1（10.4.4.12）

## 汇总

| 节点 | 主机名 | OS | 内核 | CPU | 内存 | 根盘 | Swap | 时间同步 | sudo | containerd/kubelet |
|---|---|---|---|---:|---:|---|---|---|---|---|
| A1 | VM-4-12-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 4 | 7.6 GiB | 180G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |
| A2 | VM-4-3-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 2 | 7.7 GiB | 80G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |
| A3 | VM-4-17-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 2 | 7.6 GiB | 80G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |
| S1 | VM-4-2-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 2 | 7.6 GiB | 80G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |
| S2 | VM-4-8-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 2 | 7.6 GiB | 80G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |
| S3 | VM-4-5-ubuntu | Ubuntu 24.04 | 6.8.0-124-generic | 2 | 7.7 GiB | 80G | 已启用 1.9G | 正常 | 无密码 sudo | 未安装/未运行 |

所有节点的内网地址和时区 `Asia/Shanghai` 正常。A1 CPU 为 AMD EPYC，A2 为 Intel Xeon，A3/S1/S2 为 AMD EPYC，S3 为 Intel Xeon；业务不依赖具体 CPU 型号。

## 发现的问题与下一步

1. 六台机器的供应商主机名不同，需要统一改为 `a1`、`a2`、`a3`、`s1`、`s2`、`s3`。
2. `/etc/hosts` 尚未写入六台内网地址，需要补齐，保证控制面和数据层不依赖外部 DNS。
3. Swap 当前已启用；Kubernetes 初始化前需要关闭并持久化移除 `/swap.img` 的 fstab 自动挂载。
4. containerd、kubelet 和 Docker 均未安装或未运行，符合尚未初始化集群的预期。
5. 所有节点具备无密码 sudo，可以继续执行系统基线初始化。
