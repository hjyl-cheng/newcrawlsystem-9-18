# newcrawlSystem

24.4 架构的新实现，唯一代码来源是当前仓库 `hjyl-cheng/newcrawlsystem-9-18`。
`oldjiagousys` 仅作历史行为参考；业务边界见 24.4，实施进度见 24.5 和 `ops/checks/`。

## 本地开发

使用 `.node-version` 指定的 Node 24，运行：

```bash
npm ci
npm test
npm start
```

当前只有 `services/runtime-smoke`：验证应用构建、发布和生命周期，尚无采集业务。
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
