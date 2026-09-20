# Kafka 单服务故障与恢复验收

日期：2026-09-20。范围：外围设施、小数据量、独立测试队列；未开发业务功能。

## 结果

- 三节点 Kafka 3.9.1；故障前两个既有业务 Topic 以及 `__consumer_offsets` 全部分区 RF=3、ISR=3，KRaft follower lag=0。
- 停止当时的 KRaft leader S2（10.4.4.8）上的 Kafka 服务，S1 接任 controller leader；独立测试 Topic 剩余两个 ISR，仍完成收发。
- 故障前 30 条、停止过程 31 条、服务停止期间 30 条、恢复后 30 条，共 121 条成功确认。在线消费及全新消费者从头回放均读到 121 条，正文、分区内 offset 顺序匹配，未观察到重复投递。
- 停服命令用时约 3.545 秒；重新启动到探针和全部既有 Topic 恢复三 ISR 约 6.045 秒。切换阶段单次发送最大耗时 350 ms；这些是小规模受控停服观察，不是整机断电 RTO/SLO。
- 严格测试 Topic minISR=3：正常三 ISR 时可写，停一台后写入在配置的超时窗口内没有获得确认（客户端 code=-192，Message timed out），故障期间 high offset 不变；同期 minISR=2 测试 Topic 正常收发。不宣称已演练两台同时故障。
- S2 恢复后 KRaft 三 voter 正常、MaxFollowerLag=0。恢复 timer、测试 Topic 和消费组已清理，最终仅剩两个既有 Topic 和 `__consumer_offsets`。

成功原始证据：`kafka-failover-b1611f83-1d80-4d30-a3c6-f63c4f190103.json`。
最终只读巡检：`2026-09-20-kafka-health.json`。

## 发现并修复的运维问题

初次探针准备遇到新 Topic 的元数据传播延迟及记录中的 BigInt JSON 序列化问题，发生于停服之前；已修复等待与记录格式，并清理该次残留测试 Topic。

随后第一次停 S1 时发现 Java 正常 SIGTERM 退出码 143 被 systemd 标记 failed，演练主动中止并恢复服务。失败证据 `kafka-failover-3d7f51d4-6167-4be9-a6b1-aac4f656bcd7.json` 保留原样，其中消费组清理当时返回错误；最终独立检查已确认没有残留探针消费组。

三机已安装 `ops/kafka/20-controlled-stop.conf` 对应 drop-in，改由 systemd 管理主进程停止，加入 SuccessExitStatus=143、KillMode=mixed、TimeoutStopSec=120；无需重启即可加载。`ops/scripts/install-kafka-kraft.sh` 同步新装配置。后续 S2 的停止与恢复按修正后的配置完整通过。

## 现有应用回归

演练后运行既有 `ops/ingestor/verify-validation.mjs`，使用专用验证库和既有结果队列：

- 三条合法消息均为 APPLIED，评论计数字段符合预期。
- 三条重复提交复用原回执；三条坏签名消息被 QUARANTINED。
- 权限、查询鉴权、先持久化再提交 offset、metrics 检查通过。
- 各分区新增记录 offset 58～60；最终已提交 offset=61、末尾 offset=61、lag=0，无保留缺口。消费者组两个成员正常。
- 五个 Argo Application 均 Synced/Healthy；Ingestor、Temporal、PG 入口和 runtime-smoke 均 2/2。

## 边界与下一步

没有删除既有 Topic、重置业务消费组或修改现有队列保留配置。保留上限、积压监控要求和恢复边界已写入 `ops/kafka/README.md`；结果验证队列每分区 128 MiB 的限制意味着不能承诺保留满 7 天。新增脚本是手动只读巡检，不是已上线的定时告警。

本轮不是断电、网络分区、磁盘灾难或压力测试；Kafka 数据备份/跨故障域恢复、Broker 认证与加密、集中监控仍待完成。继续按 24.12 推进 SeaweedFS 元数据/对象副本/稳定入口与恢复，再处理 ClickHouse、CDC 及监控；不要把 Kafka 本轮通过等同于外围第 4 项全部完成。

## 变更后配置备份

代码与配置提交 `5b3fbfc` 已推送新仓库 main。随后执行已部署的 control backup，生成 `control-20260920T114101Z.tar.gpg`，25,101,847 bytes，A1/S2 两份；SHA256 `d6a41a76029dc53103286df04860336447280dec9c661cc0a6876a5d3dfe98bc`。归档范围含三机 systemd drop-in、Kafka 配置、Kubernetes etcd 与 PG 协调 etcd，不含 Kafka 消息日志；本轮未重复执行此新快照的独立恢复演练。
