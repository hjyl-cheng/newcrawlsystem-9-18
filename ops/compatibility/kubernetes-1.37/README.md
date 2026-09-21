# Kubernetes 1.37 兼容性准备

这里保存官方组件的版本锁、测试 values、网络配置覆盖与 API 样例，不是新的外围服务或自研控制器。**不在 Argo 自动同步路径下，不可直接用作生产安装配置。**

## API 与结构检查

2026-09-21 使用官方 controller-tools 的 `envtest-v1.37.0`，在 A1 启动独立 kube-apiserver/etcd，仅监听本机 26443/22379/22380，独立 CA、数据目录与 kubeconfig。没有 kubelet、scheduler、业务控制器，也没有连接现网 PG/Kafka。测试完成后关闭临时进程。

| 项目 | 结果 |
|---|---|
| 官方制品 | 10 项下载成功；9 项对照官方校验和，Calico 固定 tag 原始清单另记录 SHA256 |
| Helm 渲染 | 六套 Chart 成功，加 Calico overlay 共 317 个资源 |
| CRD | 58 个在真实 1.37.0 API 注册并 Established |
| 非 CRD 资源 | 259 个 server dry-run 通过，其中 48 个为监控 CR |
| 内置类型严格 schema | 211 个通过，invalid/error/skipped 均为零；CRD 与 CR 由 API 校验，不冒充 schema 检查已覆盖 |
| 领域 CR 样例 | 9 个通过，包括 Kafka/NodePool/Connect、Argo、证书、IPPool、KEDA |
| 负例 | 删除的 Strimzi v1beta2 和非法节点角色均被拒绝 |
| 现有配置 | 八套 validation overlay 的 63 个资源 server dry-run 通过 |
| Helm strict lint | 五套通过；cert-manager 官方 Chart 的 `v1.21.2` 版本格式触发警告；普通 lint 通过 |

以上结果只表示 API 与结构检查通过；随后又完成了下述隔离运行验证。线上仍未升级。

## 隔离运行验证

在 A1 的独立 LXD project `crawl-k8s137` 中建立了两节点测试集群：Kubernetes 1.37.0、containerd 2.4.0、runc 1.5.1、Calico 3.32.2。测试网段、证书、etcd、Pod/Service CIDR 与现网分离，没有连接现网 PostgreSQL、Kafka、Secret 或业务数据。

Worker 最初限制为 1500 MiB，运行多组件时出现资源压力，因此调整为 2500 MiB 后分批验证。这是测试环境纠偏，不是生产配置结论。验证结束后已卸载测试 release/namespace、删除临时 PostgreSQL 数据并停止两台 LXD 实例；保留 project 和声明文件便于复现。

| 组件/链路 | 实测结果 |
|---|---|
| Kubernetes / Calico | 两节点 Ready；跨节点 Pod、Service、DNS、默认拒绝与选择放行策略通过 |
| Argo CD 3.5.3 | Application Synced/Healthy；手工改副本后 self-heal 恢复 |
| cert-manager 1.21.2 | Webhook 负例、签发及自然续期 revision 1→2 通过 |
| Strimzi 1.2.0 / Kafka 4.3.1 | TLS、ACL 正反例、持久卷 Broker 重启保消息/ACL、Connect REST 与重启恢复通过 |
| KEDA 2.20.2 | 指标 5 时 1→3，指标 0 时 3→1 |
| Prometheus / Alertmanager | ServiceMonitor、查询值 7、规则触发及 Alertmanager 接收通过 |
| Temporal 1.32.0 / PostgreSQL 18.6 | 建库建表、真实 workflow、四服务重启、PG Pod 重建后的历史持久性通过 |

测试中发现并修正了两个真实问题：Kafka 首次使用临时盘，Broker 重建后 KRaft 元数据与 ACL 丢失，改为 Retain 本地 PV 后才记录通过；Prometheus 3 对空 Content-Type 的抓取端点需要显式设置 `fallbackScrapeProtocol: PrometheusText0.0.4`。这些修正已经写入测试 values/清单，不能在生产配置中遗漏。

**通过范围仍有限。** 这是一个控制面加一个 Worker 的小数据兼容环境，不代表三控制面、三 Broker 整机故障 HA，也不代表生产吞吐和六节点磁盘争抢已经验证。部分上游支持矩阵仍未正式列出 1.37，本项目实测不能改写为厂商支持声明。

## 配置与发现

- `artifacts.lock.json` 固定源 URL、版本、SHA256 和官方校验结果。Calico 的 tag URL 在重新使用时必须核对已记录的 SHA256。
- `values/` 是测试参数。Temporal 使用 `.invalid` 数据库地址与不存在的测试 Secret，history shard 数沿用现网 128；不会连接现网数据库。它不包含运行所需的完整 TLS/挂载/数据库初始化配置。
- `calico/` 在官方清单上覆盖为现网 VXLAN、IPIP Never、192.168.0.0/16 和 Felix 探针，未更换 CNI 模式。只通过结构/API 检查，数据面待运行验证。
- `fixtures/` 用于 server dry-run。Strimzi 1.2.0 的 Connect 必须使用 `spec.groupId`、`configStorageTopic`、`offsetStorageTopic`、`statusStorageTopic`；旧 config 键的位置不能满足 v1 必填项。本轮实测拒绝后已修正。
- CRD 首次 create 成功，随后 create→SSA 演练出现 Strimzi `.spec.versions` 所有权冲突；没有强行覆盖。后续真实接管需单独处理字段所有权，不由本轮 API 通过推导出可直接 apply。
- cert-manager Chart 未修改，Helm 4 strict lint 的上游版本格式警告保留为已知问题。

## 复现范围与命令

下载制品放临时目录，工具不覆盖系统二进制。下列命令中的路径需对应经过校验的本地制品。**TEST_KUBECONFIG 必须指向独立测试 API，不能使用当前默认集群。**

```bash
# 示例：静态渲染，不连接集群
helm template strimzi /tmp/crawl-k8s137-compat/charts/strimzi-kafka-operator \
  --namespace kafka-compat --kube-version 1.37.0 --include-crds \
  -f ops/compatibility/kubernetes-1.37/values/strimzi.yaml

# 只对独立 API 执行；先在该测试环境建立 namespace、default SA 和官方 CRD
kubectl --kubeconfig="$TEST_KUBECONFIG" version -o json
kubectl --kubeconfig="$TEST_KUBECONFIG" create --dry-run=server \
  -f ops/compatibility/kubernetes-1.37/fixtures/custom-resources.yaml
```

API-only 环境没有 controller-manager，因此 default ServiceAccount 由测试准备显式创建。不要用统一 `-n` 覆盖 Chart 中 kube-system 等跨命名空间资源。不要向测试环境导入现网 Secret。临时证书与 kubeconfig 不提交 Git。

清理记录：本轮同时停止测试 API 与 etcd，API 退出超过 systemd 的 90 秒等待后被终止；随后确认两个测试进程退出、三个端口关闭，并清理该临时失败状态。此操作不算优雅退出验收。后续测试必须先停止 API，完成后再停止 etcd。

## 下一阶段

兼容性关卡已经完成，下一阶段进入现网 B0：重新生成 K8s etcd、PG、配置与数据备份，并实际恢复到隔离位置。只有恢复证据通过，才按 1.31→1.32→1.33→1.34→1.35→1.36→1.37 的顺序逐 minor 升级；不能从 1.31 直接跳到 1.37。

详细证据：[API 兼容性结果](../../checks/2026-09-21-k8s137-api-compatibility.json)、[隔离运行兼容性结果](../../checks/2026-09-21-k8s137-runtime-compatibility.json)。
