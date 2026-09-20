# SeaweedFS 文件副本与元数据高可用

版本固定为 4.47，二进制报告源码 `c5073360007d28385a33426a42ac3e4ec504c5a3`。本轮仅外围运维，不开发业务采集。

## 当前已部署

| 组件 | 位置 | 状态 |
|---|---|---|
| Master | S1/S2/S3，9333/19333 | 三成员；独立服务，无本机 Master 停服引起 Volume 连带停服的依赖 |
| Volume | S1/S2/S3，8080/18080 | 每个 volume 两份跨服务器副本（001）；原 1～7 号 volume 已补齐 |
| Filer | 仅 S3，8888/18888 | 目录元数据仍为 S3 本地 LevelDB2，不是 SQLite；尚未迁移 PG |
| S3 Gateway | 仅 S3，8333 | 原验证入口，无跨服务器固定高可用入口；认证待补齐 |

`001` 意味着同一逻辑 rack 中不同 Volume Server 上存两份；此处一台 VM 一个 Volume Server，能隔离单 VM 文件服务故障，不能当成异地灾备。双副本可以在三台机器中停一台时，由剩余两台承接新写入。配置两份不代表已有数据自动变成两份：本轮已执行原卷逐个修改副本策略及补副本，禁止自动删除所谓多余副本。

Master 和 Filer 默认复制策略均为 001。仍须核对后续 bucket/path 配置，避免覆盖成 000；复制策略参数不是访问控制或不可变约束。磁盘容量、volume 槽位和修复速度仍需要监控，不能由此承诺无限扩容或始终有两份健康副本。

## 操作与证据

```sh
# 小规模现状冷备，会短暂停止对象入口及文件服务，不是生产在线备份
python3 ops/seaweedfs/backup-baseline.py --execute

# 需已完成现状备份；滚动修改 Master 默认值，补齐原卷的跨机副本
python3 ops/seaweedfs/prepare-replication.py --execute

# 短停一个 Volume 服务；验证原文件可读、停机期间新文件仍获两份副本
python3 ops/seaweedfs/verify-volume-failover.py --execute
```

维护备份限制每台现有 SeaweedFS 目录小于 128 MiB；不把它用于大规模数据在线备份。先停单入口的 S3/Filer，再逐台停 Volume 后复制其文件，过程中均有远端自动恢复 timer。加密复用 control backup 的密钥，备份存 A1 受保护暂存区、S2 和 S3。除解密与归档成员校验外，已将基线恢复到全新目录，启动独立 loopback Master/Volume/Filer，恢复原 7 卷路由并通过恢复的目录读取两份历史日志，大小和 SHA256 全部一致。备份不包含一致性的 Master Raft 快照，本轮验证的是新建 Master 后卷重新注册；不是三机生产拓扑整体重建。

更改本地 Filer 之前先停止同机 S3 Gateway 的订阅连接，等待 Filer 正常退出再启动。只重启 Filer 而让长连接保持，可能使 gRPC GracefulStop 等待到 systemd 超时；不能把超时强杀当作优雅退出验证。

原始证据在 `ops/checks/seaweed-baseline-20260920T114802Z.json`、`2026-09-20-seaweed-baseline-restore.json`、`2026-09-20-seaweed-replication.json` 和 `2026-09-20-seaweed-volume-failover.json`。恢复脚本 `verify-baseline-restore.py` 需在 S3 以 root 运行，从 stdin 接收确切基线归档名和待核对文件清单；临时服务、timer、目录已清理。本轮证明文件副本与小规模维护备份可恢复，不能标记整个对象存储已高可用。

## 待实施：元数据与多入口

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

接入顺序：

1. 确认三 S 节点互通 Filer gRPC 18888，保留现有 Master/Volume 通信。
2. 在现有 PG 上创建专用 schema/表和最小权限账号；使用现有 PgBouncer，给 S 节点配置本机 loopback 入口，以 Patroni mTLS 健康检查选择唯一 Primary。限制实际后端连接，凭据不入 Git。无需把 Kubernetes ClusterIP 暴露给未入集群的 S 节点。
3. 短暂停止对象写入，保留当前 LevelDB/文件基线并导出元数据；先在一个 Filer 导入新存储，核对路径、大小、chunk 引用和读取校验值，再启动同组另外两个 Filer。不得直接切到空 PG 表导致旧对象“消失”。
4. 配置三个 S3 Gateway 的受保护认证和两份入口代理，入口需要检查后端 Filer/目录操作是否可用，不能仅看 TCP 可连接。稳定地址供集群内客户端使用；外部 Worker 的入口另按接入方式配置，不把 ClusterIP 当作公网地址。
5. 验证任一 Filer/Gateway 停止后读写和列表、PG 切主后的目录操作、跨节点会话/缓存失效，以及对象与元数据一致恢复。
6. 建立持续对象备份与保留策略，并验证独立恢复；PG 的元数据备份与对象字节备份必须配套，不能只有其中一种。切流并接收新写入后，不能直接回退到旧 LevelDB 快照。

当前 PostgreSQL 适配器的列表查询存在嵌套查询，不能盲目把客户端连接池设置很小而导致等待自身连接。具体连接池、PgBouncer 兼容模式和并发限额需实际验证；参考的上游源码固定在上述二进制 commit，不改第三方源码。

## 当前外部阻碍

S3 本机访问 `10.4.4.5:18888` 成功，服务同时监听 loopback/内网，ufw inactive；S1、S2 访问同地址超时。跨节点 Filer 订阅/元数据内部通信需要该端口；尚未部署多 Filer，也没有将流量切到未验证的入口。

轻量云防火墙需追加 **TCP 18888，来源 10.4.4.0/22，应用到 S1/S2/S3**。不是 A 节点，也不是 UDP 或对全网开放。下一次继续时先复测当前 S3 监听地址，再部署其他节点后验证双向连接。
