# 首个采集事实表 migration 验证

日期：2026-09-20。执行位置：新系统工作目录；目标：S1 的独立数据库 `crawler_schema_test_20260920_facts01`。
数据库 owner：crawler；实际 PostgreSQL：17.11。没有对正式目标 crawler 或外部 Business 应用本批 migration，没有修改旧仓库。

## 已完成

1. 编写 `database/migrations/0001_collection_facts.sql`，建立 channels（61 列）、contents（59 列）、身份约束与最小查询索引。
2. 编写 `ops/db/migrate.mjs`，实现显式目标数据库确认、主库/版本检查、会话锁、每文件事务及版本/校验和台账。
3. 用管理员 createdb 创建上述独立测试库，应用 0001，再次运行 `npm run db:migrate` 返回 applied=[]。
4. 执行 `npm run test:db`：10 项全部通过；执行 `npm test`：19 项全部通过。
5. 对照 information_schema 和旧字段映射：直接映射的频道事实已覆盖；contents.last_observation_id 延后与 Observation 外键一起实施。其他跨表拆分目标不属于本批。
6. 新增独立 GitHub 数据库 workflow，使用 CI 临时 PostgreSQL，不访问服务器。工作流 YAML 语法已检查，尚未在 GitHub 执行。

迁移校验和：`0e5919d99f13509eab6eaac941c26b3b530fae5339f74419bd02f47932b1e40b`。
台账 applied_at：`2026-09-20T02:45:55.420Z`。检查完成时 channels=0、contents=0；保留空表及台账供复查。

## 10 项数据库测试覆盖

- 版本记录、重复执行无副作用、数据库名不符拒绝。
- 已应用文件被修改时拒绝；人为失败 migration 的建表与版本记录同时回滚。
- 两个独立连接运行 migration，历史一致且执行后会话锁释放。
- 未知数量/会员标记不默认 0/false，负数和质量不匹配拒绝；bigint 最大值无 JS 精度丢失。
- video→short→live 不换主键；错误频道不能创建同视频副本；来源身份不可普通 UPDATE；帖子命名空间独立；删除频道不级联删除内容。
- 评论页缺键、JSON null、错误类型、非法日期及数量不符拒绝；SQL NULL 和合法空页允许。
- 当前评论数量/禁用状态与历史正文可独立保存，禁用却缺失数量不能漏过 CHECK。
- 频道认证、业务邮箱、加入日期、休眠/移除要求一致的证据。
- 会员/访问状态、带前缀发布 Hash、发布时间证据规则。
- 事实写入能事务回滚，评论列使用 EXTENDED 存储。

## 评论压缩核对（用户追问补充）

实际查询 `pg_attribute` / `pg_class` / `default_toast_compression`：

| 检查 | 实际结果 | 含义 |
|---|---|---|
| comments_first_page.attstorage | x | EXTENDED，允许压缩及行外存储 |
| comments_first_page.attcompression | 空 | 使用数据库默认压缩算法，未关闭压缩 |
| default_toast_compression | pglz | 当前写入默认尝试 pglz |
| contents.reloptions | NULL | 未覆盖默认 TOAST 阈值等表级参数 |
| contents.reltoastrelid | 非零 | 有对应 TOAST 附属表 |

额外在事务里插入一页 20 条、含大量重复文本的模拟评论：
`pg_column_compression(comments_first_page)` 返回 `pglz`，JSON 文本 UTF-8 大小 180744 字节，`pg_column_size` 返回 3156 字节。
这两项大小分别是文本表达和压缩后的 JSONB datum，不代表整行/索引/WAL 占用，也不能当作真实评论压缩率。
该样例仅证明自动压缩确实发生，随后整体回滚。

TOAST 按行大小和可压缩性处理，小值通常原位保存；不是每个 JSONB 都必定压缩。
没有手工 gzip/base64，没有切换到 LZ4，应用继续读写 JSONB。以后切换算法也不会自动重压历史行。

## 边界与后续

目前只有事实表结构和迁移工具；没有受控 Store、业务正文 Schema、执行授权、Submission/Receipt、Outbox 或真实 Worker 写入。
“数量更新时测试没有改正文”不等于“任意输入都不能覆盖旧正文”；非空页保留、空页补齐、过期观察拒绝须由下一步 Store 与数据库事务样例完成。
本批未做采集压力、索引执行计划、TOAST/WAL 成本对比、HA、备份恢复或 Business 接收兼容验收。
生产迁移前还需上述相关前置工作，不能把独立库成功宣称为生产链路已打通。

下一项：先实现 Store 合并规则与观察顺序测试，随后接 Plan/执行权限、Submission 和事实/检查点/回执同事务。
执行与恢复说明见 `database/README.md`；本轮发现的证据约束差异已同步 24.7 DB-12 和 24.8 第 12 节。
