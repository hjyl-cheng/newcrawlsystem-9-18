# 采集业务层

- `read-step.ts`：采集层显式控制重试预算、总时限和 HTTP 业务判断；默认一次尝试。
- `youtube.ts`：通用 YouTube.js fetch 适配与最小 getBasicInfo 读取。
- `youtube-metrics.ts`：选中存量视频的 player/next 指标采集、无损计数映射和冻结批次 Submission 组装。

```typescript
import { readYoutubeVideoMetrics, buildMetricsSubmission } from './src/youtube-metrics.js';

const result = await readYoutubeVideoMetrics(runtime, {
  channelId: task.internalChannelId,
  sourceChannelId: task.youtubeChannelId,
  sourceContentId: task.videoId,
  contentType: task.frozenContentType,
}, { signal: taskSignal, maxAttempts: 2 });

// 每个冻结目标都必须有完整结果；只有单目标任务才能直接使用下面的单元素数组。
// 数据不完整时先进入采集层后续处理，不把未知补零。
const submission = buildMetricsSubmission(frozenSingleVideoWork, [result], stableSubmissionId);
// 后续先保存 Journal，再调用现有签名 Kafka Producer；本模块不直接写库或发消息。
```

以上变量由 Worker 的可信任务提供。普通 getBasicInfo 的 Number 计数不能直接当成可入库的大整数；指标路径在 fetch 中保留原始 JSON 证据。Crawlee 会话和 IP 扩展保持各自职责，业务字段解析不进入它们。

详见根目录 `24.11_存量视频指标采集映射与提交校验.md`。首期限定英文计数表面、可播放详情和明确数字，未知不覆盖；未实现全量采集、评论正文、官方 API fallback 或终态结算。
