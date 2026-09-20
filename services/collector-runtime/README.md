# 公共采集模块

本模块加载到 Worker 进程中，不监听端口。使用 Crawlee 管理会话与代理配置，自定义扩展维护代理健康/冷却，YouTube.js 通过自定义 fetch 使用选定代理。

不修改 Crawlee 源码。`proxy-pool.ts` 只维护 IP 资源；`runtime.ts` 调用原生 Session/SessionPool，连接到 HTTP 客户端，每次只执行一次。重试、HTTP 业务状态判断和 YouTube.js 封装位于 `services/collection/src/`，不属于 IP 扩展。

完整决策、与 24.4 的差异和当前限制见根目录 `24.10_Crawlee公共采集模块与代理生命周期实施方案.md`。

```typescript
import { ProxyPool, CollectorRuntime } from './src/index.js';
import { readYoutubeVideo } from '../collection/src/youtube.js';

// definitions 由本机受保护配置或未来的平台配置客户端提供，禁止在日志打印。
const proxies = new ProxyPool('worker-01');
proxies.configure(configRevision, definitions);
const runtime = await CollectorRuntime.create(proxies);

try {
  const probe = {
    url: configuredProbeUrl,
    validate: async (response: Response) => {
      // 按选定探活接口的实际契约验证正文；不要只检查 status === 200。
      return validateProbePayload(await response.text());
    },
  };
  await runtime.probeOnce(probe);
  const stopProbes = runtime.startProbes(probe);
  try {
    // 默认一次；这里由采集层显式允许最多两次尝试。
    const info = await readYoutubeVideo(runtime, videoId, { signal: activitySignal, maxAttempts: 2 });
    // info 是 YouTube.js 解析结果，尚不是可提交的事实/Submission。
    // 业务映射、Journal、Kafka 发布放在可重试的只读回调之外。
  } finally { stopProbes(); }
} finally { await runtime.close(); }
```

这是一段调用方式示意，其中配置、验证函数和取消信号由 Worker 提供，不是可直接运行的真实采集脚本。

运行本地验收：

```bash
npm run build
node --test test/collector-runtime.test.mjs
```

测试使用本机模拟代理和 HTTPS 响应，不使用真实代理。测试依赖 Node 24 和 `openssl`，不修改系统 CA。

配置更新是单调递增版本的全量快照。新导入代理未通过探活前不可使用；全部不可用返回 `PROXY.NO_CAPACITY`。不存在自动直连兜底。取消返回 `REQUEST.ABORTED`；请求适配层原样返回 403/429 的 HTTP Response，由采集层判断、下发冷却指令并返回业务错误。解析/空数据不自动切 IP。

每个进程独占自己的代理组；多个进程不会因为 import 相同模块而共享池。当前不持久化 Cookie 或健康状态，重启后重新探活。状态可通过 `proxies.snapshot()` 汇总，包含 ID 和状态，不包含认证 URL。
