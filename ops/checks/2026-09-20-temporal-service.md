# Temporal 调度服务验证

日期：2026-09-20。结论：Temporal 双副本服务已部署，最小合成任务调度与验证 Worker 恢复通过。没有接入真实采集任务。

## 部署

- 唯一新仓库：`hjyl-cheng/newcrawlsystem-9-18`。
- 首次部署提交：`16784dd0819704cf007539a1a8a3e20acbdb2191`；启动目录修正：`a38e58af724b023d3b4410bdb7784f256da769b3`。
- `crawl-temporal-validation` 为 Synced/Healthy；检查时两个 Pod 为 `temporal-598d7c7f-lts28`（A1）、`temporal-598d7c7f-dkzdm`（A3），均 1/1 Ready、零重启。
- 官方 server 1.32.0，固定 digest `sha256:c3e752127759616bb1615e0f9ba0e21635aeb5fdeb922de4f371c350955f46ae`。
- Service `temporal:7233`，ClusterIP；没有公网 Service 或业务控制台。
- 初版由于镜像工作目录与 legacy config 路径解析方式不同启动失败，通过 Git 修正为显式 `--root /` 后由 Argo 滚动替换。没有手工修改 Deployment 绕开仓库。

## 权限与网络

- 实际运行账号可以连接两个 Temporal 库；schema 版本分别为 default 1.19、visibility 1.14。
- `has_schema_privilege(...,'CREATE')` 为 false；`schema_version` UPDATE 为 false；登录正式 `crawler` 被拒绝。
- runtime 权限为表 DML，版本账本只读；没有使用迁移 owner 作为服务账号。角色上限 64，具体授权见 `ops/temporal/runtime-grants.sql`。
- 从 A2 的临时允许客户端，连接 Service 和 A1/A3 两个 Pod 的 7233 均成功；相同节点的无授权标签客户端三条连接均超时。两个临时 Pod 已清理。
- 验证脚本通过仅绑定 127.0.0.1 的运维 port-forward 访问；验证后转发已关闭。

## 实际任务结果

脚本：`ops/temporal/verify-validation.mjs`；workflow：`ops/temporal/validation-workflows.cjs`。

- Temporal namespace：`crawl-validation`，历史保留一天。
- Workflow ID：`temporal-validation-e833e9f4-d58c-4c74-9f5a-7c893b8242a1`；独立 task queue 同名。
- 未启动 Worker 时 workflow 保持 RUNNING。
- 两个纯回显 Activity 各人为失败一次、第二次成功，总调用 4 次、预期失败 2 次。
- 第一 Activity 完成、timer 写入历史后，停止验证 Worker 实例；创建新实例后从同一 workflow 恢复，已完成 Activity 未重复执行。
- 历史中有两条 Activity 完成事件、一个 timer fired；workflow 为 COMPLETED，返回 marker 与输入完全一致。
- 停止 Worker 后，用新客户端连接仍可取回同一结果。
- 初次 namespace 注册后存在缓存传播等待；脚本已增加只针对明确 NamespaceNotFound 的有界等待，复用同一 workflow ID，不在不确定错误时另建任务。

这是 SDK Worker 实例停止/重建与持久历史恢复测试，不是进程 SIGKILL、Temporal 主机故障或 PostgreSQL 切主演练。验证 Worker 临时运行在当前服务器，并非常驻采集 Worker。

## 代码与检查

- 先前本地采集模块及其文档已提交为 `37f2e89`，Temporal SDK 依赖随根目录 lockfile 固定。
- 本轮 `npm test`：50 通过、0 失败、0 跳过；Kustomize/server dry-run、实际镜像配置模板渲染、脚本语法和 diff 检查通过。
- 提交 `16784dd` 的 CI Runtime image 与 Database migrations 两条工作流均 success，运行 ID 分别 `35500936806` / `35500936794`。
- 新 CI 应用镜像未替换原 Data Ingestor/runtime-smoke 的固定 digest；Temporal 使用独立官方镜像。四个 Argo Application 检查时均 Synced/Healthy。

## 未完成边界

当前 Temporal 显式允许无应用认证，以 namespace 内标签 NetworkPolicy 限制访问；mTLS/授权和生产监控待补。PG 仍为 S1 单写入口和异步主备，无自动切主；两个 Temporal 副本不消除 PG 单点入口。

未编写或部署业务 Planner、常驻采集 Worker、Journal、真实代理配置接口，未运行 YouTube 采集或写入外部业务库；未引入 SQLite。后续将采集步骤接成 Activity，再衔接 Kafka/PG 回执与任务终态。
