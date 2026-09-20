# 内容 Store：首个指标与评论写入切片

日期：2026-09-20。实现：`services/facts-store/src/content-store.ts`。
这是可信服务内部的事务库，尚未接真实采集 Worker。
后续 `services/ingestion` 已在同一事务内复核 Plan、执行授权与 Submission；Kafka 消费适配也已复用该事务，详见文末更新。

## 已实现的输入范围

`withContentFactsTransaction(pool, async tx => ...)` 从连接池独占一个连接，创建 READ COMMITTED 事务，成功提交、失败回滚。
回调内用 `tx.mergeContent(observation)` 合并内容；以后检查点和回执用同一个 `tx.query`，不能另开连接提交。
事务关闭后接口拒绝复用。一次事务内顺序 await 各项操作，并发请求使用不同事务。

```javascript
await withContentFactsTransaction(pool, async tx => {
  const result = await tx.mergeContent({
    channelId: 'channel-id',
    sourceContentId: 'video-id',
    contentType: 'video',
    observedAt: '2026-09-20T01:00:00.000Z',
    comment: {
      value: '120',
      status: 'exact',
      source: 'youtube-comments',
      disabled: false,
    },
  });
  // result 是当前事务内的合并结果，不是 APPLIED 回执；外围事务成功才持久化。
});
```

频道必须已经存在；此切片只接受 youtube_video 命名空间下 video/short/live。
按平台/命名空间/sourceContentId 插入或找到实体，用 FOR UPDATE 锁住同一内容行。
错误频道拒绝。已有类型与输入不同，返回 CONTENT_TYPE_CORRECTION_REQUIRED；类型纠正需要后续独立证据策略。
数据库允许有依据的类型纠正，不等于指标更新可以顺便覆盖类型。

可选输入 view / like / comment 分别更新播放量、点赞数、评论数量；每项都带 value/status/source。
view 可附原文 text；comment 可附 disabled；不接受随意追加字段。
有效 value 必须是非负 bigint 范围内的规范十进制字符串，拒绝 JS Number，避免大整数损失。
本次统一 observedAt 是这些输入指标的观察时刻，要求真实 UTC 毫秒格式；不使用数据库接收时刻冒充观察时间。
此接口是内部切片类型，不是已经冻结的新 Worker 业务正文协议。

## 指标的合并规则

| 输入与当前值的关系 | 行为 |
|---|---|
| 没有提供该指标 | 不更新该指标 |
| unresolved/unavailable 且 value=NULL | 保留旧值、旧质量、旧来源、旧观察时间；不将失败变成有效观察 |
| 有效观察时间更新 | 更新值及配套证据；数值允许真实下降，不能简单取 MAX(count) |
| 有效观察时间更旧 | 返回 older，保留当前值 |
| 时间相同，值、状态、来源及附属证据相同 | 返回 same，作为此字段的重复观察 |
| 时间相同但上述证据不同 | OBSERVATION_TIME_CONFLICT；回滚整个事务，不能按到达顺序任意覆盖 |
| 明确 disabled=true/status=disabled/value='0' | 数量为 0，保留已有正文；之后更新的有效非 disabled 数量可改变该状态 |

每个指标有自己的 observed_at。较新的播放量不会挡住另一个尚未更新的点赞数。
如果 t2 是获取失败而 t1 是有效观察，t1 仍可更新早于 t1 的有效事实；没有让 t2 的失败抹掉有效证据。
未知输入的失败详情/重试责任仍需未来 Observation/Issue 保存，当前 Store 不伪造失败流水。

## 评论正文的合并规则

page 缺失或 NULL 都表示本次没有可合并页面，不代表删除旧正文。
合法页面需通过数据库形状校验，collected_at 使用 UTC 毫秒且不得晚于该输入 observedAt。
JSON 输入拒绝不安全数字、undefined、非 JSON 对象等会被隐式转化的值；评论项语义和业务正文全量 Schema 尚待扩充。

| 当前页面 | 合法新输入 | 行为 |
|---|---|---|
| 已有非空页面 A | 页面 B | 保留 A 及其 Hash/采集时间 |
| 页面缺失 | 空页或非空页 | 保存该页面和证据 |
| 已有空页 | 更新的非空页 | 补齐正文及对应证据 |
| 已有空页 | 更新的空页 | 保留原空页，推进页面检查时间 |
| 任意页面 | 页面采集时间早于最近有效页面检查 | 返回 older，不回退页面状态 |
| 空页，同一检查时刻 | 不同页面 | 显式时间冲突，整个事务回滚 |

“第一页”是保留首先成功接受的非空页，不承诺是所有并发采集中时间最早的一页。
并发写入通过行锁串行判断；如果先提交的是 B，之后到达的 A 不会抢占已有非空 B。
当前数量可以与历史页内 total_count 不同。仅更新指标时 SQL 不重新赋值 comments_first_page，不重新计算其 Hash。

## 0002 新增的四个证据字段

- comments_first_page_observed_at：被保留页面自身 collected_at。
- comments_first_page_hash：数据库依据页面实际 JSONB 表达计算的 SHA-256。
- comments_first_page_hash_profile：固定 `pg17-jsonb-text-sha256-v1`。
- comments_page_checked_at：Store 最近接受的合法页面观察时刻，与被保留正文的时间分开。

0002 为旧页面补齐上述证据，样例升级已验证正文不变。触发器使正文 Hash/采集时间随正文一起生成；单独修改证据不能冒充页面更新。
Hash 算法是 UTF-8 的 PostgreSQL 17 `jsonb::text` 经 SHA-256 得到裸 64 位小写 hex，仅用于本地存储完整性。
它不是 JCS，不是 Submission payload_hash，也不是 publication_item_hash；不同 Hash 不能互相比较。PG 大版本升级时需验证序列化或新增 profile。
不改变评论 JSONB 的 EXTENDED/TOAST/pglz 设置，不采用手工压缩或对象外置。

## 原子性、验证与尚未覆盖的范围

一次事务内任何指标冲突、SQL 错误或后续操作失败，已合并的其他字段/内容也会回滚。
即使回调捕获了 Store/SQL 错误，事务封装仍拒绝提交。断线导致的 COMMIT_UNKNOWN 尚需外围 Submission 对账，不自动假称失败或成功。
first_seen_at 取已接受身份观察的较早值，last_seen_at 取较晚值；不推进本切片尚未证明的 player/next 整体观察时间。

24 项真实 PostgreSQL 集成测试通过，包含已有 0001 数据升级、并发首次入库、错误频道拒绝、数量/正文/Hash 独立、缺失/空页补齐、旧观察和同时间冲突、整个事务回滚。
测试代码：`test/database/content-store.test.mjs` 与 `collection-facts.test.mjs`。

尚未实现：频道 Store、内容标题/描述/发布时间等完整字段策略、类型纠正、帖子写入、Observation/Plan 引用、授权、Submission/Receipt、Outbox、最小权限角色及对外服务。
已有行的直接 SQL owner 权限仍可绕过 Store 正文保留规则；当前不是已部署的权限隔离边界。
应用骨架镜像仍是 runtime-smoke，本模块没有线上入口。下一步把 Plan/授权/Submission 与事实/检查点/回执接进同一个事务。


后续实施更新（2026-09-20）：`services/ingestion` 已用本 Store 完成局部计划/授权/Submission 与批次检查点/APPLIED 原子事务；事务封装现在对 COMMIT 确认异常返回 DB.COMMIT_UNKNOWN，外围提供原身份回执查询。范围、测试与限制见 `metrics-submission.md`。

Kafka 接入更新：`services/kafka-results` 已完成签名结果的消费与重放验证，Kafka record 的 APPLIED 记录也与本 Store 写入同事务提交，成功后才提交 offset。
不改变已有非空评论页保留、空页补齐、评论数量独立更新与 TOAST/pglz 规则。生产凭据/权限、常驻服务及完整业务字段仍未实现，见根目录 24.9。
