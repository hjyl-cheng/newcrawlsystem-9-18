# 集中基础设施日志

六台现有主机的 Vector 0.58.0 → S3 ClickHouse HTTPS 8443 → Grafana 官方 ClickHouse 插件 4.21.3。没有新数据库服务器、SQLite、Loki、Rota 或业务采集代码。

## 范围与部署

- Vector 作为 systemd 服务运行，每台一份。收集明确白名单的 systemd 单元，A 节点再读取 crawl-validation / crawl-monitoring / argocd / kube-system 的 CRI 文件；S 节点读取 PG/PgBouncer 当前日志文件，S3 增加 ClickHouse warning/error 文件。不是全磁盘扫描，Vector 自身日志不回流到自身。
- 主机文件读取需要 root，但配置了只读系统、私有临时目录、隐藏敏感配置目录、单线程、CPU 30%、内存 256Mi、IOWeight 10。只有日志代理自己的目录可写；业务服务不依赖 Vector。
- S3 新建 `crawler_logs.events`，与 `crawler_analytics` 分离。六个专用 INSERT 账号各限来源 IP；Grafana 专用 SELECT 账号限 A 节点来源，并限制单查询线程/内存/时间/结果量。TLS 验证 CA 和主机名，不跳过验证。
- 腾讯轻量云只需 S3 TCP 8443 来源 `10.4.4.0/22`。其他节点无需新增日志入站端口。Vector 指标只监听本机 9598，经现有 mTLS node_exporter 9100 汇总；Grafana NetworkPolicy 只增加到 S3:8443。
- Grafana 插件使用官方签名 Linux amd64 ZIP，下载时校验官方 SHA256，预置到三 A 节点版本目录，以只读 hostPath 挂载。Grafana 仍是原固定官方镜像。节点替换时必须先恢复插件目录再调度 Grafana；缺目录时 Pod 应失败，不自动下载 latest。

## 丢失、重复与容量边界

这是可丢失的诊断日志链路，不是业务事实投递通道。不能以本链路替代 PG Outbox/Kafka/业务回执。

- 每节点非心跳日志最多 200 条/10 秒，超额丢弃；单行输入上限 32KiB，正文最多 4096 字符，保留截断标志。
- 先做字段白名单、密码/token/API key、HTTP Authorization、Cookie、URL 用户信息等模式脱敏，再进入落盘缓冲；不保存整份 journal 字段。模式覆盖不等于能识别任意格式秘密，应用仍不得主动记录明文凭据。CRI `P` 片段被丢弃，不做跨行/片段重组，普通 `F` 行可查。
- 每节点磁盘缓冲上限 512MiB，满时 `drop_newest`，不让采集日志反压业务进程；后台重试网络失败。永久 4xx/格式拒绝会丢弃，发出计数告警；不是所有错误都无限重试。
- 磁盘缓冲默认约 500ms 同步；代理骤停可能丢失尚未落盘的数据，断点和确认窗口可能产生重复。源日志若在读取前被轮转删除，也无法补回。没有 exactly-once 承诺。
- 首次 journal 从当前开始；文件仅跟踪最近一小时修改的白名单文件，从文件头读取，并丢弃超过三天/未来五分钟的事件。上线前历史不是完整归档。
- 中央表按 UTC 小时分区，ZSTD 压缩，TTL 三天。S3 每五分钟检查：活跃压缩数据超过 2GiB，按最旧小时分区清到约 1.5GiB；磁盘可用低于 15% 时可清空这张派生日志表，为同机服务腾空间。容量优先于三天保留，当前小时也可能被删。
- **2GiB 是周期清理阈值，不是硬磁盘配额**。新写入、合并临时文件、非活跃 parts 会产生额外占用；TTL/删除回收有延迟。现有系统盘共置仍是验证环境边界，不等于 24.4 要求的生产独立盘已满足。
- ClickHouse 仍为单机。S3 故障时六台代理按有限缓冲策略积压，Grafana 日志查询暂停；恢复后重试补传缓冲内的日志。缓冲耗尽、S3 磁盘损坏、日志源轮转均可能丢日志。

`crawler_logs` 不纳入每日 `crawler_analytics` 业务分析备份，短期诊断数据不承诺灾备恢复。日志 schema、采集/保留配置、账号/TLS、Grafana 数据源与查询面板纳入新仓库及现有控制面加密备份；证书有效期两年，自动轮换仍待后续安全收尾。

## 操作

```sh
python3 ops/logging/fetch-artifacts.py
python3 ops/logging/prepare-backend.py --execute
python3 ops/logging/render.py
python3 ops/logging/test-vector.py
python3 ops/logging/test-retention.py
python3 ops/logging/install-agents.py --execute
python3 ops/monitoring/render.py
```

Vector 0.58 默认禁止环境变量插值。安装脚本通过 YAML 结构化赋值将密码写入远端 root:0600 配置文件；Git 中只保留占位模板，不启用危险插值开关，不在命令参数中传密码。轮换密码时先更新受保护 secrets/logging 文件，再部署后端账号与节点配置。

Grafana 的数据源和日志面板随现有 `crawl-monitoring` Argo 应用发布。命名空间 Secret `grafana-logs-private` 由后端脚本建立，不入 Git。

入口仍使用原 Grafana SSH 隧道，进入“基础设施 / 爬虫平台 · 集中日志”，按服务器、服务、时间和正文关键词查询；默认最多 200 条。Explore 可选择 `Infrastructure Logs`。查询账号没有 INSERT/DDL 或业务表读取权限。

排查时先看日志心跳新鲜度、缓冲量、丢弃/错误计数；再看 `journalctl -u crawl-vector`、S3 `crawl-log-retention` timer 和 ClickHouse。排查输出不得复制受保护配置的明文密码。Grafana 凭据、监控原有密码保持不变。

Grafana 插件连接检查会调整 `max_execution_time`。该账号保持 readonly=1，仅此设置允许在 1–60 秒内修改；单线程、128MiB 查询内存、结果条数/大小上限保持约束。Grafana 请求默认超时 10 秒。该例外不授予写入或 DDL 权限，已做实际拒绝验证。

现场验收：`python3 ops/logging/verify.py --execute`；如首次只在注入故障前中断，可在 30 分钟内用 `--resume-outage` 继续已记录的四个前置阶段后的演练。它只临时阻断 A3 主机到 S3:8443，预装四分钟自动恢复定时器，finally 删除专用规则。`test-buffer-policy.py` 用隔离的极小内存缓冲验证 drop_newest；没有填满线上 512MiB 磁盘缓冲或进行压力测试。
