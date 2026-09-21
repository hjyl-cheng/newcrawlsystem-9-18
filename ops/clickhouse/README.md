# ClickHouse 备份与独立恢复

当前 S3 (`10.4.4.5`) 运行 ClickHouse 26.8.6.5 单节点，数据在 `/var/lib/clickhouse`。本轮仅外围运维，不编写分析投递器或业务表。`crawler_analytics` 仍没有业务表，临时验证表已清理。

## 已部署链路

```text
S3 在线 ClickHouse：crawler_analytics
  │ 原生 BACKUP DATABASE（带表结构、数据与校验信息）
  ▼
S3 受保护 native 临时目录
  │ SSH 传输，校验原生文件 SHA256
  ▼
A1：AES256 加密、解密复核、ZIP CRC 校验
  │ 密文 SSH 复制 + SHA256 核验
  ▼
S2：独立服务器的加密副本
```

原生备份通过新增 `crawl_backups` Disk 热加载接入，未重启在线 ClickHouse。备份线程 2、备份 IO 线程上限 4、服务器备份带宽上限 16 MiB/s。A1 的 `crawl-clickhouse-backup.timer` 每天 **Asia/Shanghai 04:10** 开始，随机延后最多 5 分钟，Persistent=true；服务超时 20 分钟。A1/S2 各保留最近 7 个完成的完整备份，手工运行也计入这 7 个。

密文在 `/srv/crawlsystem/backups/clickhouse`；密钥复用现有受保护 control backup 密钥，不进入 Git。`last-success.json` 保存最近成功时间和校验值。新密文校验并跨机落地后才轮换旧备份、删除本次 S3 原生临时文件。失败的 staging/partial 文件保留排查，需管理员确认后清理；磁盘空间不足会拒绝启动新备份。

这是原生在线备份，不要求全库停服。不能据此声称多表具备单个跨表事务快照。范围仅 `crawler_analytics`，不含 system 日志、其他数据库或用户配置；服务器/账号配置由已有加密 control backup 保存。

## 运维命令

```sh
# 从本仓库部署备份 Disk 和任务文件；此命令不启用 timer
python3 ops/clickhouse/prepare-backup.py --execute

# 创建专属临时探针，测试备份与隔离恢复，finally 清理探针
python3 ops/clickhouse/verify-backup-recovery.py --execute

# 恢复验证通过后启用；首轮已执行
sudo systemctl enable --now crawl-clickhouse-backup.timer
sudo systemctl start crawl-clickhouse-backup.service

# 只读检查：源库可查询、备份 ≤30小时、A1/S2 密文校验、timer与上次任务结果
sudo python3 /opt/crawlsystem/backup/check-clickhouse.py
```

健康检查目前按需运行，尚未接入集中监控或外部告警。A1 停机不会影响在线分析服务，但会暂停新的定时备份；现有 S2 副本仍保留。定时器本身不是 HA 调度器。

## 已验证的独立恢复

验证在 S3 新目录启动独立 ClickHouse 进程，HTTP 18123/Native 19001 仅 loopback，独立配置/数据目录，运行期上限 300 秒、MemoryMax=1 GiB。这些端口不需要新增云防火墙规则。恢复使用同一固定版本，既不覆盖在线目录，也不连接在线实例写入恢复数据。

验证表包含中文、毫秒时间、Nullable、Array、Decimal、多版本记录，备份内 3 行完整恢复；在线备份后新增 event_id=999 被排除，在线表仍保留 4 行。表清单/引擎与原生备份一致。验证完成停止独立进程，删除恢复目录和临时表。约 2.18 秒是这份极小探针恢复的观测，不能作为生产 RTO 承诺。

详见 `ops/checks/2026-09-21-clickhouse-backup-recovery.json` 和 `ops/checks/2026-09-21-clickhouse-health.json`。

## 与 24.4 的差异及尚未验收项

24.4 §23.10 允许首期 ClickHouse 单节点共机，但明确要求独立数据盘/资源预算，并禁止在没有余量时与 PG/Kafka/SeaweedFS 强行共用高争用盘。**现场 S3 仍只有系统盘，这一项不满足生产要求**。现有查询内存/线程参数和本次备份限额，不等于完整 cgroup CPU/Memory/IO 隔离已验收。此轮按用户限定的小数据搭建验证执行，不能把“后续升 CPU/内存”当作独立磁盘、故障域和 IO 隔离的替代。

没有部署 Keeper、ReplicatedMergeTree 或第二台 ClickHouse。因此整台 S3 故障时，历史分析会暂停，需恢复/迁移才能提供服务；不具备自动切换。PG 核心事实不依赖 ClickHouse，但分析 Outbox 投递/退避/积压背压/回填代码尚未开发，不能声称已证明业务端自动追平。

每日备份恢复点取决于最近一次成功快照；快照之后的数据需另行回放来源，目前该业务链尚未落地。A1/S2 均在现有同地域/账号故障域，跨机副本不等于异地灾备。整机损坏恢复、容量压力、外部存储、长期保留、持续告警和生产 RPO/RTO 均仍待验收。

官方参考：https://clickhouse.com/docs/operations/backup 。实际命令在已安装的 26.8.6.5 上完成验证。
