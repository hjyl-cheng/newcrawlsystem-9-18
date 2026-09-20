# Data Ingestor 常驻服务验收

日期：2026-09-20。结论：两个消费者副本已通过新仓库 CI 镜像和 Argo CD 部署；真实 Kafka/PG、受控查询、网络边界和滚动重放验证通过。
这是增量视频指标的常驻验证服务，不是完整生产采集系统。

## 发布与部署证据

| 项目 | 实际值 |
|---|---|
| 唯一新仓库 | hjyl-cheng/newcrawlsystem-9-18 |
| 镜像源码提交 | 6255a8832b05d381cec469c3841a5c121b3bd650 |
| 镜像 | ghcr.io/hjyl-cheng/newcrawlsystem-runtime@sha256:0c7f06781034ec89cad90cd0e0a4d8bbc1894503fe22a012360c45a78ae8302e |
| 初次部署提交 | 28f36f0291b13d9295140e85f66968eb187d50d7 |
| 滚动验证提交 | 597fb2c45c9e79ea7d06a27ba22fc88a072a7dff |
| Argo Application | crawl-ingestor-validation，Synced / Healthy |
| 命名空间 / Service | crawl-validation / data-ingestor:8080，ClusterIP |
| 实际数据库 | S1 crawler_validation_ingestor |
| Topic / Group | crawler.results.validation.v1 / crawler-results-apply-validation-v1 |

CI 两条流程均成功：

- [应用测试与镜像发布](https://github.com/hjyl-cheng/newcrawlsystem-9-18/actions/runs/35489075383)：27 项测试通过；镜像构建额外检查 pg 和原生 Kafka 客户端能在最终运行镜像中加载。
- [全新 PostgreSQL 迁移与数据库测试](https://github.com/hjyl-cheng/newcrawlsystem-9-18/actions/runs/35489075389)：database job 成功，与六台服务器隔离。

Argo history 的首次部署时间为 2026-09-20T04:27:01Z，滚动版本为 04:29:15Z。
初始副本位于 A1/A2，滚动后为 A1/A3：`data-ingestor-564dff74b5-ckcnl` / `data-ingestor-564dff74b5-z9jzh`，均 Ready、零重启。
新副本分别分配 2/1 个分区；三个 A 节点仍 Ready，原 infra/runtime 两个 Application 仍 Synced/Healthy，runtime-smoke 原镜像 digest 未改。

## 数据库和权限

专用常驻验证库与 crawler_schema_test_20260920_facts01 分开，避免测试清理影响常驻服务。
0001～0004 的 SHA-256 与先前测试库一致，依次在 04:18:53.777Z / .781Z / .800Z / .806Z 应用；正式 crawler 与外部 Business 未执行本批迁移或写入。

运行账号 crawler_ingestor_validation，每实例 pool=4、账号连接上限 16；仅有当前切片所需的事实列、提交、回执、检查点和处理记录权限。
实际 PG 验证以下操作均拒绝：建表、删回执、写计划、更新计划/频道协调/执行授权/逻辑批次、修改不属于切片的内容 title。
SELECT FOR UPDATE 所需的列 UPDATE 授权由额外 statement trigger 阻止真实控制表更新，零行 UPDATE 也拒绝。
S1/S2/S3 的 HBA 均限制该账号只从内网连接验证库；对三台服务器的正式 crawler 登录尝试均被拒绝。
完整授权配置位于 `ops/ingestor/validation-runtime-grants.sql`，不是偷偷修改已有 migration。

签名私钥、查询 token 和随机 PG 密码保存在本地忽略目录；消费者 Pod 只得到专用 PG 凭据、公钥/权限登记和查询 token 的 SHA-256。
没有把签名私钥、查询明文 token、迁移 owner 密码或 K8s ServiceAccount token 注入消费者。

## 真实链路和接口结果

1. 本机服务先用真实依赖通过三个分区的合法/重复/坏签名消息，每分区 offset 0～2；SIGTERM 后 draining/stopped，退出码 0。
2. 集群部署后再次执行 `verify-validation.mjs`：每分区 offset 3～5；合法消息 APPLIED、重复消息复用原回执、坏签名保存 QUARANTINED 原文，数据库成功后才提交 offset。
3. 无 token/错误 token 返回 401；没有频道权限返回 403；合法请求先得到 NOT_OBSERVED，处理后得到同身份 APPLIED。运维可查隔离原因/原文大小，不直接返回原文。
4. 从 A3 的允许客户端访问 Service 和两个 Pod，健康/版本/metrics 均通过；另一个没有访问标签的 Pod 请求超时，NetworkPolicy 拦截生效。
5. 通过 Git 修改 Pod 模板的验证标记，Argo 滚动替换两个副本，同时按原 Submission 连续重发 **135 条**消息。`verify-restart.mjs` 验证 3 个回执完全不变、每个提交仍只有一份业务回执、视频评论数量未改变，全部 offset 已追平。
6. 滚动后从允许客户端再次检查 Service 和两个新副本：各分区 lag=0、retention_gap=0、blocked_partitions=0；分区分配为 2/1。

最终验证库保留 6 个合成内容和 6 个业务回执，消息结果为 APPLIED 147 条、QUARANTINED 6 条。消息处理次数包含重复投递，不等于新增内容数。
保留这些小样例作为常驻链路证据，不删除正在使用的 stream/offset 台账。临时查询 Pod 已清理。

## 资源与验收范围

每副本 requests 100m/128Mi、limits 500m/384Mi、JS heap 上限 192 MiB。
轻量重放后的 `/proc/1/status` RSS 分别为 85472/84652 kB，约 83 MiB/副本；只是本次小样例观察，不是峰值容量或压力测试结论。
集群尚未安装 Metrics API，本次内存值直接读取容器主进程，不冒称 kubectl top 或完整监控数据。

Kafka 仍是内网 PLAINTEXT，Broker SASL/TLS/ACL 未完成；PG/HTTP 也未做生产 TLS。应用签名、查询鉴权和 NetworkPolicy 不代替传输加密或 Broker 身份隔离。
指标尚未接 Prometheus 抓取与告警；真实 Worker、Local Journal、Temporal、完整计划结算、隔离回放及生产备份/HA 仍待后续。
现有 PG 异步主备与验证期 API 入口的限制仍存在；滚动更新成功不等于已完成主机故障或数据恢复验收。

操作和回滚见 `ops/ingestor/README.md`：应用通过 Git 调整 digest/模板，保留 Kafka 队列和数据库，不回滚已有 migration。
