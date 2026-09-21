# Data Ingestor 常驻验证服务

唯一代码源为本仓库，服务入口 `services/data-ingestor/src/main.ts`。
两副本部署于 A 集群，消费 S1/S2/S3 专用 Results Topic；经稳定连接池入口写当前 PG 主库中的独立 `crawler_validation_ingestor`，不使用正式 crawler，也不复用会被测试清理的 schema_test 库。
本目录的创建/授权脚本只适用于该验证数据库，服务启动不会创建计划、迁移表或自行授权。

## 凭据与数据库准备

1. `node ops/ingestor/create-validation-credentials.mjs`：仅首次生成。私钥、随机 PG 密码、查询 token 写入忽略目录 `secrets/data-ingestor`，目录 0700、文件 0600；已存在时拒绝覆盖。
2. `python3 ops/ingestor/bootstrap-validation-database.py`：经既有 SSH 管理通道创建验证 DB/专用账号，配置 S1/S2/S3 上该账号的 PG HBA 边界。账号不能连接正式 crawler。HBA 原文保留 `.conf.pre-ingestor` 备份；这不是数据库备份。
3. 用迁移 owner，设置 PGDATABASE/DB_EXPECTED_NAME 均为 crawler_validation_ingestor，运行 `npm run db:migrate`，再执行 `validation-runtime-grants.sql`。
4. 将 runtime.env 创建为 Secret `data-ingestor-database`；trusted-keys.json/query-clients.json 创建为 Secret `data-ingestor-identities`，均位于 crawl-validation。**不上传 worker-private.pem、operator.json 或迁移 owner 密码到消费者 Pod。**

专用 runtime 账号最多 16 个连接，每进程 pool=4。允许当前事实列和 Submission/Receipt/Checkpoint/record outcome 的必要写入，不允许 DDL、删除、写频道/计划/执行授权或任意内容字段。
PG 的 SELECT FOR UPDATE 要求 UPDATE 权限，因此少数控制表仅授一个列的 UPDATE，另以 BEFORE STATEMENT 触发器拒绝该账号的所有实际 UPDATE；已验证零行 UPDATE 也拒绝。此为验证期角色专属授权脚本，后续通用角色模型需版本化，不把它当跨环境自动迁移。
账号仍是受信服务账号：权限限制不能代替签名、授权与 Store 业务检查，也不宣称数据库可以判断每条 SQL 是否遵循合并策略。

Kafka 仍是现有内网 PLAINTEXT；消息有 Ed25519 签名，Broker SASL/TLS/ACL 尚未部署。HTTP token 仍只用于受限内网，不能直接开放公网；PG 通道已通过原生 PGSSLMODE=verify-full / NODE_EXTRA_CA_CERTS 验证 CA 和入口名称，PgBouncer 前后两段均启用 TLS。见 `ops/postgresql-ha/APPLICATION-SQL-TLS.md`。
当前只有一个验证签名身份和一个运维查询身份，限定三个模拟频道；真实 Worker 的自动发证、动态频道权限和轮换流程尚待后续。

## 构建与 GitOps 部署

`npm test` 验证协议、签名、HTTP 权限、请求上限和就绪撤销。
Docker 构建同时验证生产依赖中的 pg 和原生 Kafka 客户端能加载；runtime-smoke 继续使用原 digest。
推送新仓库 main 后，GitHub Actions 发布 `newcrawlsystem-runtime:sha-<commit>`。

```bash
python3 ops/scripts/pin-runtime-image.py <source-commit> data-ingestor
# 提交、推送生成的 digest overlay 后：
kubectl apply -f deploy/argocd/bootstrap/project.yaml
kubectl apply -f deploy/argocd/bootstrap/data-ingestor.yaml
```

Argo Application 为 crawl-ingestor-validation；Service 为 data-ingestor，ClusterIP 8080，未开放公网端口。
每副本 requests 100m/128Mi，limits 500m/384Mi，JS heap 192 MiB；包含 Kafka native 内存的实际 RSS 仍须监控，不能把 heap 上限当总内存。
Pod 跨 hostname 分散，PDB minAvailable=1，滚动 maxUnavailable=0/maxSurge=1。不是完整机房/节点故障 HA 验收。

NetworkPolicy 仅允许同命名空间且带 `ingestor-query-client=true` 的 Pod 访问 HTTP；出口仅 DNS、S1 PG、三 Broker 9092。
管理员 port-forward 使用 Kubernetes 管理通道；标签和 token 均不应由非可信工作负载随意获得。
运行时无 K8s API token，以非 root/只读文件系统执行。Secret 中的信任登记和查询客户端在进程启动时加载；变更后需通过 Git 改动 Pod 模板的非敏感版本标记触发滚动。

## 查询、健康与监控

- `GET /health/live`：进程可响应，不依赖 PG/Kafka。
- `GET /health/ready`：启动完成、最近依赖检查成功、无保留缺口、无本实例待重试分区；依赖采样过期也撤销就绪。
- `GET /version`：源码提交/版本。
- `GET /metrics`：需要运维 Bearer token；包含本进程 APPLIED/隔离/重试次数、分区分配、依赖采样时间、消费组各分区 lag 和保留缺口。
- `POST /v1/receipts/lookup`：需要有目标频道权限的 token；请求为 `{submission_id,identity:{plan_id,channel_id,logical_batch_key},content_sha256}`。返回原 APPLIED 或 NOT_OBSERVED，冲突 409、无权 403。
- `POST /v1/records/lookup`：仅运维 token，请求 `{partition,offset}`；Topic/stream 固定为本实例配置。返回处理结果/原因/原文大小，不直接暴露隔离原文，不提供修改/回放接口。

所有查询 body 上限 4 KiB、并发上限 8；异常不输出凭据、请求正文或 SQL。指标中的 lag 是同组共享视图，抓取两个副本时应按 partition 取 max，不应相加；处理次数包含幂等重放，不等于新增视频数。
已接基础设施就绪探针、Kafka 积压与平台内告警；本服务专用 metrics 仍需运维 token，未来业务指标按正式业务上线补齐。
SIGTERM 先撤销就绪并停止消费，等待进行中的回调和查询，50 秒强制退出上限，Pod 宽限 60 秒；未确认 offset 可重放。

## 真实验证与回滚

用管理员 port-forward 或允许的验证客户端访问服务，再设置 owner PG 环境变量及 INGESTOR_URL，运行 `node ops/ingestor/verify-validation.mjs`。
该脚本只向验证库的三个固定模拟频道准备新计划，发送合法/重复/坏签名消息，核对回执、字段、隔离、权限和消费进度；保留有明确前缀的少量模拟证据，不清理正在消费的 stream。
owner 凭据仅在受控验证脚本中使用，不注入服务。摘要写在本地忽略目录 last-acceptance.json，公开验收记录不含凭据。

回滚应用采用 Git revert digest/模板提交，让 Argo 同步；不删除 Topic、重建 stream 或回滚数据库 migration。
首次部署前无旧消费者版本时，可在 Git 将该 Deployment replicas 设为 0，保留队列、库和 Secret。恢复前核对保留窗口和 lag。
正式 crawler、Business、Temporal 和真实 Worker 不在本次上线范围。

滚动验证可使用 `verify-restart.mjs`，读取上次合成样例并按原身份重发，`REPLAY_ROUNDS` 为 1～100；过程中由 Git 修改非敏感 Pod 模板标记触发更新。查询入口须在滚动期间保持可用（Service/受控验证客户端），不要把固定旧 Pod 的 port-forward 当作稳定服务入口。

已完成的发布与滚动证据见 [2026-09-20 验收记录](../checks/2026-09-20-ingestor-service.md)。

SQL 运维验证请先配置当前主库身份与 CA，并按 `ops/postgresql-ha/APPLICATION-SQL-TLS.md` 使用 with-sql-tls.py；旧的无 TLS 连接已被拒绝。
