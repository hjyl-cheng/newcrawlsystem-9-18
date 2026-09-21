# 入库客户端在 Kafka 切换时的崩溃修复

## 现场问题

Kafka 新增 TLS listener 的滚动过程中，两份旧 Ingestor 各发生3次容器重启。Kubernetes恢复了进程，后续样例入库正确，但这不能当作稳定的故障接管。

旧镜像中的 `@confluentinc/kafka-javascript=1.10.1` 使用 `librdkafka=2.15.1`。现场上一实例日志显示：

```text
rdkafka_queue.h:1086: rd_kafka_enq_once_del_source_return:
Assertion `eonce->refcnt > 0' failed.
```

容器退出码139。该特征与上游 [librdkafka #5397](https://github.com/confluentinc/librdkafka/pull/5397) 报告的 coordinator-targeted Admin 请求断连错误路径一致；当前npm最新仍为1.10.1，未把未合并的第三方C补丁直接加入生产镜像。

## 变更

消费进度/保留边界检查从 `Admin.fetchOffsets(groupId)` 改为当前消费者的 `Consumer.committed(明确分区列表, 5000)`，避开受影响的Admin请求路径。范围端点仍用Admin ListOffsets；缺口检查、SQL事务、消息签名和持久化后提交offset的顺序保留。

SDK会将未设置offset和数值0均表示为null，两者在本系统都按next=0验证；低水位已大于0时仍拒绝启动，不允许跳过丢失数据。返回缺少指定分区、格式异常或超出SDK安全整数范围时失败；连接/超时错误继续阻止就绪或触发原分区重试。

启动顺序调整为连接消费者后再验证进度、最后subscribe/run。测试新建topic时明确等待实际ListOffsets低/高水位均为0，不能只凭metadata已显示ISR3认定已可读取。

这是外围故障恢复暴露出的客户端兼容性修复，不恢复Planner/真实Worker/代理UI等业务开发。

## 发布与测试

- 源提交：`1bf699fab222201102aee6589ef43dcee486fdaf`。
- 镜像：`ghcr.io/hjyl-cheng/newcrawlsystem-runtime@sha256:ca4518a071e2878cdd932c5febaec36d36b1ba910c344b6949e10919257a8534`。
- 发布清单提交：`7dc3b12`，由原Argo应用滚动更新两个Ingestor。
- CI Runtime image run `35573612474` 的50项测试及镜像构建成功。
- 本地真实Kafka/独立测试PG库的9项测试通过，覆盖双消费者并发、坏消息隔离、PG事务回滚、确认后重放和保留缺口拒绝；没有删除正式topic或更改正式业务库。
- `ops/kafka/verify-ingestor-reconnect.py --execute` 用Kafka FindCoordinator确定当前结果消费组协调节点，受控停一个Kafka服务并设4分钟救援timer；故障期间持续检查两个Pod身份/restartCount，并在故障和恢复后各执行真实入库回归。最终结果见同名JSON。

首次修复后演练停止S3，两个进程都未重启，但30秒处未满足就绪检查，脚本恢复节点；记录保留在 `2026-09-21-ingestor-kafka-reconnect-first-attempt.json`。随后按最长90秒的有界恢复窗口观察，定向停止当时的协调节点S1，5～30秒采样均就绪，故障和恢复后各9条入库样例通过，Pod UID不变且restartCount均0；见 `2026-09-21-ingestor-kafka-reconnect.json`。没有因此承诺所有节点/网络故障均能在30秒内恢复。

迁移应用到mTLS后，脚本已改用9094并另存 `2026-09-21-ingestor-kafka-tls-reconnect.json`，不覆盖前述旧入口演练的证据。

此修复绕开已观察到的问题路径，不等于修好了上游所有native客户端缺陷。一次性运维脚本仍有Admin消费组查询，不应将其嵌入常驻业务进程；它们在节点切换时可能失败，需要在进程边界重试。新入口9094的应用迁移与ACL另行验收。
