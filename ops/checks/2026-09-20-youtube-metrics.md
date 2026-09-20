# 存量视频指标映射本地验收

日期：2026-09-20。

- 实现：`services/collection/src/youtube-metrics.ts`。
- 首轮定向检查：23 项通过；发现合成 fixture 使用了当前 YouTube.js Menu 不接收的旧 renderer，随后改成其支持的 ToggleButton fixture，保留映射器的旧形状兼容读取。
- 最终 `npm test`：50 项通过，0 失败、0 跳过；无上述 parser warning。
- `git diff --check`：通过。

新增 10 项映射测试，并扩展原 CONNECT/TLS 集成测试。测试证据覆盖原始超大整数、字段与任务身份、缺失/关闭/冲突、冻结目标、无正文提交、现有入库契约及 Kafka 签名编解码。

真实 YouTube.js getInfo 经本地 CONNECT 代理发出 player/next 请求，目标由本机 HTTPS fixture 响应。响应含未加引号的 9007199254740993 数值，最终指标仍保留完全一致的十进制字符串。请求列表证明未调用评论 continuation。

本轮无真实外部采集、无 Kafka 写入、无 PG 连接、无集群部署。签名编解码使用测试现场生成的临时 Ed25519 密钥；未读取生产/验证环境凭据。旧系统仅做只读字段规则参考。

不完整计数暂不封存为完整批次；后续业务 fallback 和 Journal 尚未实现，不能以本地映射通过代替真实采集成功率和链路恢复验收。
