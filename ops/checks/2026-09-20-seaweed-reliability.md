# SeaweedFS 第一阶段可靠性验收

日期：2026-09-20。当前结论：文件双副本、受控 Volume 停服和维护基线独立恢复验证完成；目录元数据 HA、多 S3 入口、持续备份与完整集群灾备未完成。

## 现场发现

- Master 和 Volume 各三份，但原 7 个 volume 均为复制策略 000，即单份内容；三台机器安装 Volume 不等于每个文件有三份副本。
- S3 上单 Filer，元数据位于 `/srv/crawlsystem/seaweedfs/filer/filerldb2`，为 LevelDB2。并未引入 SQLite。
- 原 S3 桶列表为空；目录中保留两份 SeaweedFS 内部历史事件日志，未发现实际业务采集对象。保留了原目录与日志，没有清空存储。
- Volume/Filer 原 service Requires 本机 Master，可能因本机 Master 维护造成不必要的连带停服。
- Filer gRPC 18888 在 S3 本机可用，S1/S2 连接超时；S3 ufw inactive，监听正常。跨节点连接尚未具备。

## 已完成的改变

1. 短暂停止入口与文件服务，完成原卷文件、LevelDB 与配置的维护基线备份。加密归档 `seaweed-baseline-20260920T114802Z.tar.gpg` 共 7,800 bytes，SHA256 `68334cb7e3047804f2b64cf8d0853f94f604005fef5e559d1400ee26fd258c5d`；A1、S2、S3 各一份。解密及逐成员 SHA256 通过，后续独立恢复见下文。
2. 三个 Master 的默认副本策略改为 001，S3 Filer 同样设为 001。原 volume 1～7 逐个升级复制策略后执行受限补副本，不删除副本；现场共 14 份卷副本，分别分布 S1=5、S2=5、S3=4。
3. 解除 Volume/Filer 对单个本机 Master 的强停服依赖，解除 S3 对本机 Filer 的停服级联。服务维护时显式管理先后顺序；更新新装脚本和持久 systemd drop-in。
4. 维护 Filer 时发现长连接订阅影响优雅退出，显式停止同机 S3 订阅后恢复 Filer，再启动 Gateway；所有服务最终 active，入口 HTTP 恢复。

## 小文件故障验证

- 256 KiB 测试文件存于 volume 5，确认 S1/S2 各有一份。
- 停止 S1 的 Volume 服务，Master、PG、Kafka 等保持运行；等待 Master 拓扑移除 S1 后，旧文件仍读取成功，SHA256 匹配。
- 故障期间写入另一份 256 KiB 文件，落到 volume 6，确认 S2/S3 各一份，读取校验成功。
- S1 Volume 恢复后，原 volume 5 双副本重新注册；两份测试文件再次读取并核对通过。
- 实测停服约 10.547 秒、恢复注册约 0.790 秒；不是断电故障 RTO。测试路径和恢复 timer 已清理，额外的单次检查文件也已删除。

原始证据：`2026-09-20-seaweed-volume-failover.json` 和 `2026-09-20-seaweed-replication.json`。

## 独立恢复

在 S3 全新目录解密维护基线，恢复三个节点归档的 7 个原卷和 S3 LevelDB。临时 Master 19334、Volume 18081、Filer 18889 及各自 gRPC 端口只监听 127.0.0.1，使用独立配置、独立 Master 和原卷副本，不连接正在运行的生产 Master/Volume。

经恢复后的 Filer 路径读取两份原有历史日志（2472、1300 bytes），大小及 SHA256 与现场原文件一致；新 Master 重新注册 7 个卷，所有路由均指向独立 loopback Volume。服务、目录及自动清理 timer 均已移除，未覆盖原数据。

证据：`2026-09-20-seaweed-baseline-restore.json`。这是停写维护基线的小规模恢复，不是多节点在线一致备份、持续对象备份或异地灾难恢复；恢复出的基线仍为备份时的 000，线上已部署的 001 未被回退。

## 明确未完成

目录元数据仍在 S3，本轮没有切换到 PG，也没有启动多 Filer；S3 Gateway 仍为单入口。完整对象存储 HA、签名认证/传输边界、持续备份、PG 元数据与对象配套恢复、磁盘与副本告警均需后续实施。

已确定接下来的实现方向：目录元数据放入现有 crawler 数据库的独立 `object_metadata` schema；多 Filer/S3 Gateway 配合跨 A 节点的稳定入口，详见 `ops/seaweedfs/README.md`。这是 24.4 对象存储实现细化，不新增数据库服务器、不改变 Business DB。

继续前需在轻量云防火墙向 S1/S2/S3 允许来源 `10.4.4.0/22` 的 **TCP 18888**。规则应用后复测双向 Filer 通信，再继续迁移与验收；不把现有双副本结果记成外围第 4 项整体完成。
