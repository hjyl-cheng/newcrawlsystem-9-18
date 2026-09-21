# SeaweedFS 共享元数据、认证多入口与配套恢复验收

日期：2026-09-21。仅外围基础设施；原始 JSON 与运维脚本均提交新仓库，不改老系统或业务采集代码。

## 已部署

- S1/S2/S3：Master、Volume、共享同一 PG schema 的 Filer、带账号认证的 S3 Gateway。
- Filer 元数据位于现有 `crawler.object_metadata.filemeta`，通过每台 S 的 loopback HAProxy 15432 → 当前 Primary PgBouncer 6432；Patroni mTLS `/primary` 检查选择主库。未新增数据库服务器或本地 SQLite。
- A 集群：两个跨节点 `seaweed-s3-entry` Pod，固定 Service `seaweed-s3.crawl-validation.svc:8333`，Argo Application `crawl-seaweed-s3-validation`。不要求腾讯云浮动 VIP。
- S3 使用受保护随机密钥，Worker 仅能使用 `crawl-validation-objects` 桶。内部 Filer/Volume/Master 仍依赖内网边界，尚未完成全面认证/TLS。

## 证据与结果

| 项目 | 结果 | 原始证据 |
|---|---|---|
| 18888 | S1/S2/S3 三方 TCP 全互通 | 本轮节点连接探测；服务间订阅建立 |
| 旧目录迁移 | 17 条原路径存在；7 个文件大小/SHA256 一致 | `2026-09-21-seaweed-metadata-migration.json` |
| S3 权限与读写 | 三节点 signed PUT/GET/LIST 与跨节点覆盖/删除可见性；匿名/错误密钥/越桶请求拒绝；桶列表按账号过滤 | `2026-09-21-seaweed-s3-auth.json` |
| Filer/Gateway 故障 | S1 两服务停止后真实 Service 上传/读取/列表成功，恢复后成功 | `2026-09-21-seaweed-service-failover.json` |
| 入口替换 | 删除一个入口 Pod，新请求仍通过 Service 完成；最终双份 Ready | 同上 |
| PG 计划切主 | S1→S3，首个成功完整读写往返约 4.6 秒；1 次请求失败，最终恢复 S1 | `2026-09-21-seaweed-pg-switchover.json` |
| 新配套备份 | 全体对象服务暂停约 23.15 秒，冻结 PG 元数据与 Volume 文件，密文 A1/S2/S3 三份 | `2026-09-21-seaweed-pg-objects-backup.json` |
| 独立恢复 | 新 PGDATA 和 loopback 文件服务恢复；52 行元数据指纹一致；19 个非内部日志文件大小/SHA256 一致；11 个卷各恢复一份 | `2026-09-21-seaweed-pg-objects-restore.json` |

备份 `seaweed-pg-objects-20260921T013904Z.tar.gpg`，27226 字节，SHA256 `fe512681e33ea3510941ce3dcee204efb5ae6535585eebc2a4cee7cfb2c0cecc`。文件很小是因为当前只有验证对象，不是吞吐或容量测试。解密密钥与 S3/PG 凭据不入 Git。

## 迁移与版本处理

固定 SeaweedFS 4.47 源码 commit `c5073360007d28385a33426a42ac3e4ec504c5a3`。导出跳过内部日志，使用保留旧 LevelDB 冷快照逐条导出补齐；导入 EOF 不等待最后文件异步操作，追加已有目录记录迫使等待完成，随后独立比对内容。原 LevelDB 和快照留存，不允许切流后自动回退到旧目录。

S 节点发行版 HAProxy 2.8 不支持 3.2 的 `init-state`：PG 选择器使用预置 DOWN server-state 文件；Kubernetes 固定镜像 HAProxy 3.2 使用 `init-state fully-down`。两种入口均需健康检查确认后才接入。

故障注入用独立恢复 timer 及 finally；S1 Filer 因长订阅连接在 stop 请求后强制结束主进程。此前调试时发现辅助进程信号返回错误以及 systemctl 多单元输出空行导致误判，已改为定向主进程和过滤空行。成功证据仅来自最终完整通过的执行。PG 恢复偏好主库时临时要求两台同步副本，使原主成为同步候选，再恢复 strict sync=1；未降低同步写入要求。

备份测试会短暂停止对象服务，故不作为线上持续备份方案。原 `backup-baseline.py` 不包含 PG 元数据，已增加迁移后拒绝执行保护。新恢复过程不连接在线 PG/Master/Filer，不把重复 volume ID 同时挂入恢复服务；完成后临时服务/目录已清理。

## 未完成边界与下一步

当前证明小规模拓扑可用、受控单服务故障后接管、计划切主后恢复与维护备份可恢复。未证明整机断电、网络分区、多机故障或生产负载下的恢复指标。11 个卷有跨机双份部署，容量/卷槽位需监控，不能认为增加 Worker 就无限扩容。

后续仍需持续对象备份/保留、生产在线一致恢复、内部接口认证/TLS与告警、外部 Worker 接入方式。ClickHouse 仍单机；CDC、监控安全收尾和 Argo controller 整机失联自动接管仍有待办。下一步按 24.12 继续外围，不恢复业务代码开发。
