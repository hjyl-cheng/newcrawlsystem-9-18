# newcrawlSystem

24.4 架构的新实现，唯一代码来源是当前仓库 `hjyl-cheng/newcrawlsystem-9-18`。
`oldjiagousys` 仅作历史行为参考；业务边界见 24.4，实施进度见 24.5 和 `ops/checks/`。

2026-09-21 当前优先事项：先完成[外围成熟组件选型与替换清单](24.15_外围成熟组件选型与替换清单.md)，其中第 6 节盘点已开发代码与上线状态。暂停新增业务功能及外围自研控制器；组件迁移须单独验证，不把选型当成已部署。

第一批业务通信契约见 [24.6 说明](24.6_业务通信协议与数据契约_第一阶段.md)。
`contracts/data-plane/` 保存 Worker/Ingestor 提交与回执的草案 Schema 和样例，
`npm test` 同时检查契约和运行骨架；其中指标提交切片已接入常驻验证服务，完整契约仍未作为生产协议定案。

数据库表设计见 [24.7 讨论稿](24.7_数据库表结构设计_单库与事务边界讨论稿.md)，
包含字段、约束、事务、评论存储及分批建表范围；第 16 节记录实际实现与架构的差异和待验证事项。首个事实表 migration 已在独立验证库执行，正式目标库尚未应用；说明见 [数据库执行指南](database/README.md)。

采集事实逐字段映射见 [24.8 核对记录](24.8_采集事实表逐字段映射与约束核对.md)，覆盖五张旧表的 173 个字段，附约束和评论更新验收目标。

采集结果入口已按用户确认改为 **Worker → Kafka Results → Data Ingestor → PostgreSQL**，见 [24.9 实施方案](24.9_Kafka采集结果入口与并发入库实施方案.md)。
`services/kafka-results` 提供签名生产、消费组并发、入库后提交进度和坏消息隔离，复用现有授权与 Store；9 项真实 Kafka/PG 测试通过。
这是可复用库与验证链路；常驻 Data Ingestor 消费者已部署两个副本，真实采集 Worker 尚未部署。Publication 仍走独立 Topic，正式 crawler 和外部 Business 未应用本批迁移。

## 本地开发

使用 `.node-version` 指定的 Node 24，运行：

```bash
npm ci
npm test
npm start
```

当前部署包含 `services/runtime-smoke` 和两个 Data Ingestor 验证副本，尚无真实采集 Worker。以下 8080 健康/版本描述适用于原 runtime-smoke 骨架；Data Ingestor 的接口与部署见文末运行手册。
新增内部库 `services/facts-store` 已完成首批指标/评论事务合并；`services/ingestion` 已接最小授权/Submission/检查点/回执原子事务，均通过真实 PG 测试，并由 Kafka 消费者内部调用。见 [Store 说明](docs/migration/content-store.md)与[受控提交说明](docs/migration/metrics-submission.md)。
默认监听 8080，提供 `GET /health/live`、`GET /health/ready`、`GET /version`。
`PORT` 可配置；镜像构建时写入 `APP_REVISION`、`APP_VERSION`。
SIGTERM 后撤销就绪，等待 3 秒传播，再关闭监听，10 秒内完成退出。
未来服务的 readiness 必须按各自必要依赖定义，不能把此无依赖探针当成数据库健康证明。

## 构建与发布

GitHub Actions 在 PR 上编译、测试，在 main 代码变动时测试后发布到
`ghcr.io/hjyl-cheng/newcrawlsystem-runtime:sha-<完整 Git SHA>`。
CI 使用仓库内置 `GITHUB_TOKEN`，无需把个人密码或 SSH 私钥放进工作流。
工作流摘要记录镜像 digest；部署应固定该 digest，更新 validation overlay 后交给 Argo CD。
第一次发布后须确认 GHCR 包允许集群拉取；公开 GitHub 源码不代表镜像包自动公开。
基础镜像、直接依赖和 Actions 版本均固定，升级通过 Git 审查。

镜像仅包含编译产物和 Node 运行时，以非 root 用户运行。
验证服务只开放集群内部 Service，不提供公网业务入口。
后续领域契约、Store、Temporal、Data Ingestor 按 24.5 顺序增量实现。

常驻 Data Ingestor 验证服务入口为 `services/data-ingestor`，提供受保护回执/处理记录查询、依赖健康与 lag 指标；部署/凭据/回滚说明见 [运行手册](ops/ingestor/README.md)。使用独立常驻验证库，不与自动测试库混用；正式业务链路尚未上线。
