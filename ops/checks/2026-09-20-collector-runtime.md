# Crawlee 公共模块本地验证

日期：2026-09-20。

实现范围：`services/collector-runtime`，独立代码模块、加载到 Worker 进程；没有部署额外服务，也没有连接真实代理或真实 YouTube。

## 检查结果

- `npm run build`：通过。
- `node --test test/collector-runtime.test.mjs`：初次 9 项通过；随后增加全部代理失效和响应体超时用例。
- 初版 `npm test`：38 项全部通过，其中公共模块 11 项、既有测试 27 项。
- 职责修订后 `npm test`：40 项全部通过，新增请求层不自行重试和业务状态由采集层处理两项验证。
- `git diff --check`：通过。

本地测试使用真实 HTTP CONNECT、TLS 握手、Undici ProxyAgent 和 YouTube.js 17.2.0 的 getBasicInfo。两个代理、探活目标及 YouTube 响应均为 loopback fixture，代理只允许固定测试目的地址，并将其连接到本机测试服务器；未调用外部 YouTube 服务。

证明的行为：

1. 新代理探测前不可分配，配置校验失败不会部分生效。
2. 连接失败后换到另一个已探测代理，重建 Crawlee 会话和 YouTube.js 实例。
3. 已失败代理不再被后续操作选中，全部失败后有界结束、不绕过代理。
4. 空数据与解析失败不自动触发换代理。
5. 429 进入目标冷却，普通探活成功不会解除冷却；403 不作为全局 IP 故障。
6. 并发采集会话的 Cookie 独立，代理认证不泄露给目标网站。
7. 旧探测成功不能覆盖新失败，停用的既有绑定在下一请求前被拒绝。
8. 取消、读取响应体超时和大小限制会释放占用；调用方取消不惩罚代理健康。
9. 实际 YouTube.js 能通过注入 fetch 解析本地视频响应，返回错误视频 ID 时拒绝。
10. 请求适配层只执行一次，只有采集层显式指定重试预算才会重复执行。
11. IP 扩展不判断 HTTP 业务状态；采集层判断 403/429 后显式设置目标冷却。

职责修订：Crawlee 源码保持原版依赖；Session/CookieJar 使用原生能力。YouTube.js 封装和有限重试移到 `services/collection/src/`，不再从 IP/请求适配模块中导出。

## 未验证/未实施

真实代理质量与 YouTube 通过率、跨机器代理配置唯一归属、代理管理 UI、动态配置网络客户端、Worker Journal、实际视频指标到签名 Submission 的无损映射，以及本模块到 Temporal/Kafka 的常驻 Worker 接入。

未变更现有 Kubernetes/Argo 服务、数据库 schema 或 Kafka Topic；本轮未运行不相关的数据库/Kafka 集成测试。此前 Temporal 依赖及 `ops/temporal` 的未提交准备工作保留。

依赖审计的已知 stream-json moderate 公告及使用边界见 24.10；本轮未跨主版本强制覆盖依赖。
