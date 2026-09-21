# SeaweedFS 文件副本与元数据高可用

版本固定为 4.47，二进制报告源码 `c5073360007d28385a33426a42ac3e4ec504c5a3`。本轮仅外围运维，不开发业务采集。

## 当前已部署

| 组件 | 位置 | 状态 |
|---|---|---|
| Master | S1/S2/S3，9333/19333 | 三成员；独立服务，无本机 Master 停服引起 Volume 连带停服的依赖 |
| Volume | S1/S2/S3，8080/18080 | 每个 volume 两份跨服务器副本（001）；原 1～7 号 volume 已补齐 |
| Filer | S1/S2/S3，8888/18888 | 同组共享现有 PG 的 crawler.object_metadata.filemeta；旧 LevelDB2 保留为迁移快照 |
| S3 Gateway | S1/S2/S3，8333 | 已开启账号认证；Worker 账号限制到验证桶 |
| 固定入口 | A 集群跨节点两个 HAProxy Pod | seaweed-s3.crawl-validation.svc:8333；Argo CD 管理 |

`001` 意味着同一逻辑 rack 中不同 Volume Server 上存两份；此处一台 VM 一个 Volume Server，能隔离单 VM 文件服务故障，不能当成异地灾备。双副本可以在三台机器中停一台时，由剩余两台承接新写入。配置两份不代表已有数据自动变成两份：本轮已执行原卷逐个修改副本策略及补副本，禁止自动删除所谓多余副本。

Master 和 Filer 默认复制策略均为 001。仍须核对后续 bucket/path 配置，避免覆盖成 000；复制策略参数不是访问控制或不可变约束。磁盘容量、volume 槽位和修复速度仍需要监控，不能由此承诺无限扩容或始终有两份健康副本。

## 历史基线与副本操作

```sh
# 旧 LevelDB2 基线脚本仅保留历史参考；迁移到 PG 后已增加拒绝执行保护。
# 当前 PG + 文件联合维护备份见下文。

# 需已完成现状备份；滚动修改 Master 默认值，补齐原卷的跨机副本
python3 ops/seaweedfs/prepare-replication.py --execute

# 短停一个 Volume 服务；验证原文件可读、停机期间新文件仍获两份副本
python3 ops/seaweedfs/verify-volume-failover.py --execute
```

维护备份限制每台现有 SeaweedFS 目录小于 128 MiB；不把它用于大规模数据在线备份。先停单入口的 S3/Filer，再逐台停 Volume 后复制其文件，过程中均有远端自动恢复 timer。加密复用 control backup 的密钥，备份存 A1 受保护暂存区、S2 和 S3。除解密与归档成员校验外，已将基线恢复到全新目录，启动独立 loopback Master/Volume/Filer，恢复原 7 卷路由并通过恢复的目录读取两份历史日志，大小和 SHA256 全部一致。备份不包含一致性的 Master Raft 快照，本轮验证的是新建 Master 后卷重新注册；不是三机生产拓扑整体重建。

更改本地 Filer 之前先停止同机 S3 Gateway 的订阅连接，等待 Filer 正常退出再启动。只重启 Filer 而让长连接保持，可能使 gRPC GracefulStop 等待到 systemd 超时；不能把超时强杀当作优雅退出验证。

原始证据在 `ops/checks/seaweed-baseline-20260920T114802Z.json`、`2026-09-20-seaweed-baseline-restore.json`、`2026-09-20-seaweed-replication.json` 和 `2026-09-20-seaweed-volume-failover.json`。恢复脚本 `verify-baseline-restore.py` 需在 S3 以 root 运行，从 stdin 接收确切基线归档名和待核对文件清单；临时服务、timer、目录已清理。以上是迁移前的历史验证；当前共享 PG 和多入口的结果见下文。

## 当前链路：元数据与多入口

```text
集群内 Worker / 运维验证客户端
              │
    seaweed-s3 固定 Kubernetes Service
              │
    两份跨 A 节点的入口代理
              │
       S1 / S2 / S3 S3 Gateway
              │
       S1 / S2 / S3 Filer
          ┌───┴───────────────────┐
          │                       │
     文件目录元数据           文件内容和 chunk
          │                       │
  现有 PG 主备集群             三台 Volume
  crawler.object_metadata      跨机器双副本
  （独立 schema）
```

这里只新增现有 `crawler` 数据库中的 `object_metadata` schema 和受限账号，不增加数据库服务器、不分片、不改变外部 Business DB。独立 schema 是 SeaweedFS 内部目录索引，业务事实、回执仍使用原权威表；SeaweedFS 元数据操作也不是跨组件的 PG 业务事务。PG 自身出现不可用时，依赖目录元数据的对象请求也可能失败，不能宣称彻底消除依赖。

实施顺序与验收范围（1～4 已部署，第 5 项服务故障/PG 计划切主已验证；持续备份、安全加固仍待收尾）：

1. 确认三 S 节点互通 Filer gRPC 18888，保留现有 Master/Volume 通信。
2. 在现有 PG 上创建专用 schema/表和最小权限账号；使用现有 PgBouncer，给 S 节点配置本机 loopback 入口，以 Patroni mTLS 健康检查选择唯一 Primary。限制实际后端连接，凭据不入 Git。无需把 Kubernetes ClusterIP 暴露给未入集群的 S 节点。
3. 短暂停止对象写入，保留当前 LevelDB/文件基线并导出元数据；先在一个 Filer 导入新存储，核对路径、大小、chunk 引用和读取校验值，再启动同组另外两个 Filer。不得直接切到空 PG 表导致旧对象“消失”。
4. 配置三个 S3 Gateway 的受保护认证和两份入口代理，入口需要检查后端 Filer/目录操作是否可用，不能仅看 TCP 可连接。稳定地址供集群内客户端使用；外部 Worker 的入口另按接入方式配置，不把 ClusterIP 当作公网地址。
5. 验证任一 Filer/Gateway 停止后读写和列表、PG 切主后的目录操作、跨节点会话/缓存失效，以及对象与元数据一致恢复。
6. 建立持续对象备份与保留策略，并验证独立恢复；PG 的元数据备份与对象字节备份必须配套，不能只有其中一种。切流并接收新写入后，不能直接回退到旧 LevelDB 快照。

当前 PostgreSQL 适配器的列表查询存在嵌套查询，不能盲目把客户端连接池设置很小而导致等待自身连接。具体连接池、PgBouncer 兼容模式和并发限额需实际验证；参考的上游源码固定在上述二进制 commit，不改第三方源码。

## 2026-09-21 本轮验证

TCP 18888 已验证三 S 节点互通。元数据迁移核对原 17 个条目全部存在，7 个文件大小/SHA256 相同。原 LevelDB2 和冻结快照保留，不允许自动回切过时目录。

4.47 的 `fs.meta.save` 跳过 `/topics/.system/log` 子树；本次从旧快照逐条补导。`fs.meta.load` 在 EOF 前不等待最后文件的异步写入；导入文件末尾附加已有目录记录，利用目录处理的等待逻辑完成导入，并再核验路径与内容。不改上游二进制。

S 节点自带 HAProxy 2.8 不支持 3.2 的 `init-state`，因此 PG 选择器使用预置全 DOWN 的 server-state 文件，启动后通过 Patroni mTLS `/primary` 检查才接入。只监听本机 `127.0.0.1:15432`，连接当前主库的 PgBouncer 6432。当前节点既有的 PG 主备和 Kafka 保持原部署。

三网关 signed PUT/GET/LIST、跨节点覆盖更新/删除可见性、匿名拒绝、错误密钥拒绝、Worker 越桶拒绝与过滤桶列表已验证。账号密钥保存在受保护文件，未提交 Git。Kubernetes NetworkPolicy 只放行同命名空间标记 `object-store-client=true` 的客户端到固定入口；入口检查网关匿名返回 403 和同节点 Filer 的真实目录读取成功。

真实 A2 客户端 Pod 通过 Service DNS 验证了 S1 Filer/Gateway 停止、恢复、一个入口 Pod 替换后的上传/下载/列表。Filer 的长订阅连接需要在本次故障注入中强制结束主进程，属于受控进程故障，不是整机断电演练。PG S1→S3 计划切主后约 4.6 秒完成首个成功读写往返，观察到一次请求失败。最终恢复 S1 Primary 和 strict sync=1；恢复阶段临时要求两台同步副本，使原主库有资格安全切回。

```sh
python3 ops/seaweedfs/verify-gateway-auth.py
python3 ops/seaweedfs/verify-service-failover.py --execute
python3 ops/seaweedfs/verify-service-failover.py --execute --pg-switchover
python3 ops/seaweedfs/backup-pg-objects.py --execute
```

证据：`ops/checks/2026-09-21-seaweed-{metadata-migration,s3-auth,service-failover,pg-switchover}.json`。

## 当前备份与边界

`backup-pg-objects.py` 只适用于每节点 Volume 目录小于 128 MiB 的搭建验证阶段。全体网关/Filer/Volume 短暂停服后，同步保存 PG `object_metadata` 的逻辑转储和三台 Volume 文件，并校验冻结期间元数据指纹未变；独立恢复 timer 与 finally 负责恢复。加密文件分别保留 A1/S2/S3。这是人工维护备份，不是已经上线的生产持续备份。

`verify-pg-objects-restore.py` 在 S3 新目录启动隔离 PG、Master、Volume、Filer；从备份中每个 volume 选一份完整副本，避免把重复 ID 挂载两次。校验完整元数据表指纹，再通过隔离 Filer 读取冻结前清单中的非内部日志文件。临时服务只监听 loopback，与在线服务无数据目录或数据库连接共享；测试结束删除临时目录。

后续仍需：对象持续备份/保留策略、增长后在线一致恢复、内部 Filer/Volume/Master 的认证与 TLS、外部 Worker 访问方式、容量/延迟/副本告警。S3 身份认证不等于所有内网接口已加固。此轮单服务故障与计划切主通过，不代表任意整机/网络分区/多机故障均已验收，也不代表整个外围计划已完成。
