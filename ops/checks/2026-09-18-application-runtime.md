# 最小应用骨架与镜像发布验证

## 范围

唯一代码仓库：`hjyl-cheng/newcrawlsystem-9-18`；本地目录：`~/workspace/newcrawlSystem`。
本次实现 `services/runtime-smoke`，仅验证应用运行、镜像构建和 GitOps 发布。
尚未实现 Planner、Query、Temporal Worker、Data Ingestor、业务 Store 或采集任务。

## 已实现

- Node 24.21.0、TypeScript 5.9.3；公共 npm 地址和完整 lockfile。
- HTTP 存活、就绪、构建版本接口；JSON 生命周期日志。
- SIGTERM 先撤销就绪，3 秒后关闭监听，10 秒退出上限；K8s 终止宽限 15 秒。
- 多阶段镜像，仅携带编译产物，非 root、只读根文件系统、移除 capabilities。
- CI：PR 编译测试，main 代码变动后测试并推送 GHCR；Actions 和基础镜像按 SHA/digest 固定。
- Argo Application `crawl-runtime-validation`，同一验证命名空间，内部 ClusterIP Service。
- 两副本跨节点调度；每副本 requests 25m/64Mi、limits 250m/128Mi，仅为骨架验证参数。
- `pin-runtime-image.py` 核对镜像内源码提交和版本，生成固定 digest 的 overlay。
- `check-runtime.py` 从 A1/A2/A3 的探针检查每个应用副本和 Service。

## 已验证证据

本地与 CI 的三项测试通过：健康/版本与就绪撤销、无效端口启动失败、SIGTERM 收尾退出。
最初构建发现本机默认腾讯 npm 镜像地址不能作为通用 CI 输入，已修正为公共 npm registry。
另修正了 CI 版本读取的引号与失败传播，后续核对镜像 APP_VERSION 非空。

首个验收镜像：

```text
源码：92dcfb5958de025ecf3f9416690ef1e4fb1d5241
版本：0.1.0
镜像：ghcr.io/hjyl-cheng/newcrawlsystem-runtime@sha256:137b0e05c6631c1726fd92efc66010b2f2a9a6ea951655922020b30efd114d88
CI：https://github.com/hjyl-cheng/newcrawlsystem-9-18/actions/runs/35344361653
部署提交：7a540f14418a9abbc5199cc7f799b6865584bfdf
```

实际拉取 GHCR 镜像成功，首版副本位于 A2/A3，均 Ready、无重启。
Argo CD 为 Synced/Healthy；三个 A 节点到两个副本和 Service 的九组健康/版本验证全部通过。

## 镜像更新与回滚实测

第二版仅提升版本号，用于演练完整镜像更新：

```text
源码：6915ae1e39df48770497bb8036d8797b774f7cf6
版本：0.1.1
镜像：ghcr.io/hjyl-cheng/newcrawlsystem-runtime@sha256:686519af6766a49dc9a0410debc8d57b862dec416c5fee544a7dd595a0a6e74e
CI：https://github.com/hjyl-cheng/newcrawlsystem-9-18/actions/runs/35344558738
部署提交：5ad2dca74665ff3e98299f14a6892f34ebfabcbe
回滚提交：570ca52d073e8458ff10816e4fd9a316625e875b
```

1. Argo CD 将 0.1.0 滚动升级为 0.1.1；九组健康/版本验证全部通过。
2. 对部署提交执行 `git revert` 并推送；Argo CD 自动恢复 0.1.0 镜像。
3. 回滚后九组健康/版本验证全部通过；两个副本位于 A2/A3，Ready、零重启。
4. Argo history 留下三次同步：`7a540f1 → 5ad2dca → 570ca52`；两个验证 Application 均 Synced/Healthy。

最终 validation overlay 保留已验证的 0.1.0 镜像。源码最新 package 版本为演练用 0.1.1，
构建与部署分开提升；当前实际部署来源以 digest 和 `/version` 的源码提交为准。
全过程没有手工修改运行中的应用配置，也没有使用 `kubectl rollout undo`。

## 验收边界

应用无存储依赖，健康接口通过不代表业务数据链路已完成。
当前 Kubernetes API 仍以 A1 为验证入口，Argo CD 单副本；PG 未启用自动切主，备份和生产 HA 待完成。
本次不证明采集吞吐能力，也不将骨架的资源上限应用于未来业务服务。
后续先实现领域契约、migration 和 Store 幂等约束，再接 Temporal/Data Ingestor 最小业务闭环。
