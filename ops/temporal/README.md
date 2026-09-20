# Temporal 验证服务

用途：执行和恢复任务编排。当前只验证合成任务，不是采集 Worker，不连接代理、YouTube、Kafka 或采集事实表。

## 部署边界

- 官方 `temporalio/server:1.32.0` 固定 digest；两个跨节点 Pod，每个运行 frontend/history/matching/内部 worker。内部 worker 是 Temporal 自身组件，不是我们将要开发的采集 Worker。
- 每副本 requests 100m/256Mi、limits 1 CPU/768Mi、Go 软内存预算 512Mi；这些是验证配置，不是生产容量结论。
- 使用同一逻辑 PG 集群中已有 `temporal_validation` 和 `temporal_visibility_validation`；同一 PG 实例中的专用服务库，不是新数据库服务器，不对采集事实分片。
- 128 个 history shard 在初始化后固定；它是 Temporal 任务历史的内部分区，与采集数据库分片无关。不能在已有持久化库上随意修改。
- 两个 SQL store 各 maxConns=2/maxIdleConns=1；每个服务会建立自己的池，账号上限 64 为双副本及滚动期间预留。真实用量需查询 PG；不能把 2 当成整个集群的连接数。
- 模板中的环境变量只在进程内渲染，数据库凭据由已有 `temporal-database` Secret 提供。运行 Pod 不获得 owner Secret、Kubernetes API token 或采集系统私钥。
- 镜像 1.32.0 与旧 docker-builds 的入口不同，没有旧 `/etc/temporal/config/config_template.yaml`。本项目提供自己的配置，已用实际镜像的 `--config ... --env server render-config` 验证模板；不依赖旧镜像自动建库流程。
- 服务入口 `temporal.crawl-validation.svc.cluster.local:7233`，仅 ClusterIP，无公网入口。原生 gRPC 健康探针检测 WorkflowService。
- 本验证明确使用 `--allow-no-auth`，没有启用 Temporal mTLS/业务身份授权。NetworkPolicy 仅放行同命名空间 `temporal-client: "true"` 的 Pod 和 Temporal 内部通讯，不能替代生产认证；有该标签的客户端是受信运维/Worker，而非普通业务用户。临时本机验证通过受控 kubectl port-forward。
- 出站仅允许 DNS、Temporal 同组节点以及 postgresql-entry。数据库使用 `postgres-rw.crawl-validation.svc:5432`，经 HAProxy 直达当前主库，不经过事务连接池。PG 自动切主证据见 PG HA 验收记录；生产安全、监控告警、独立角色扩容仍未完成。
- Temporal 不需要本地持久卷保存任务历史，历史由 PG 保存。这不解决未来采集 Worker 的 Journal 磁盘问题，也没有引入 SQLite。

## 首次搭建顺序

1. `python3 ops/temporal/bootstrap-validation.py`：准备专用库/账号和 HBA；密码仅写入忽略目录。
2. 将 owner/runtime env 分别通过 `kubectl create secret generic --from-env-file=... --dry-run=client -o yaml | kubectl apply -f -` 写入 `crawl-validation` 的 `temporal-schema-owner` / `temporal-database`。不在终端打印 Secret。
3. 单独应用 `ops/temporal/schema-job.yaml`，等待成功。已有 schema 不重复执行 setup-schema；升级需另做版本匹配的 update-schema Job。
4. 在 S1 以 postgres 执行 `ops/temporal/runtime-grants.sql`。应用只有 DML，schema_version/schema_update_history 只读。未来官方迁移新建表后，重新核对并授予运行权限。
5. 校验 `kubectl kustomize deploy/overlays/validation/temporal` 及 server dry-run，将部署文件提交推送至唯一新仓库。
6. 应用 `deploy/argocd/bootstrap/temporal.yaml`，由 Argo CD 创建配置、Service、Deployment、NetworkPolicy 和 PDB。ConfigMap hash 变更触发滚动。

## 无外部采集的验证

本机需要根目录 lockfile 对应的 Node 24/Temporal SDK，已有 Kubernetes 运维权限。

```bash
kubectl -n crawl-validation port-forward service/temporal 17233:7233 --address 127.0.0.1
# 另一个终端：
node ops/temporal/verify-validation.mjs
```

脚本只接受 loopback 地址；按需创建 `crawl-validation` Temporal namespace，保留历史一天。每次使用唯一 workflow ID 和独立 task queue，不消费正式任务。

验证内容：没有 Worker 时任务保持 RUNNING；启动临时 SDK Worker 后执行纯回显 Activity；每个 Activity 人为失败一次、第二次成功；等待历史中 timer 落盘后关闭临时 Worker，再创建 Worker 恢复同一个 workflow；检查两次 Activity 完成、一个 timer、结果一致，最后重新连接查询持久化结果。

失败只终止本脚本创建的 workflow；不清库、不删 namespace。总验证时限 180 秒，workflow 自身上限 2 分钟。临时 Worker 运行在当前服务器，不是 Kubernetes 常驻采集 Worker。验证后关闭 port-forward。

## 维护与回滚

- 以 Git 修改部署配置，Argo 同步；不要手工改变运行 Deployment 绕开仓库。
- 发布前确认 server 版本与两个官方 schema 匹配；不将镜像降级等同于数据库降级。
- 需要暂停服务时，以 Git 将 replicas 设为 0；保留 PG 库、schema 和任务历史。
- PDB 保护自愿驱逐；两个应用副本之外，PG 已接入 Patroni 同步主备和固定入口；切主期间 SQL 连接会中断并重建，不能承诺零停顿。
- 本次不创建业务 Planner、采集 Journal、代理管理 UI 或完整任务终态结算。
