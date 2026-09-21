# CDC 单节点故障自动恢复

2026-09-21，接续首次 CDC 验收，修复“坏一台备库就长期卡住”和“旧主返回的本地槽阻止原生同步”两项问题。仍使用现有 PG17/Patroni/etcd，不新增服务器或 Redis，不修改业务采集代码。

## 部署与职责

S1/S2/S3 各运行 `crawl-cdc-guard.service`，postgres 用户、root-owned 程序 `/opt/crawlsystem/cdc/ha-guard.py`；内存上限 128 MiB，CPU 上限单核的 20%，每轮结束后间隔 2 秒。它只管理精确探针槽 `crawl_cdc_validation` 的安全接管条件。

Patroni 仍负责选主、严格同步写入、旧主降级和 watchdog；PostgreSQL 仍负责原生槽同步。guard 不自行 promote，不推进逻辑 LSN，不接管业务消息。

每台 Patroni 增加 `postgresql.pre_promote`。官方调用点在“已获得 DCS leader lock、尚未提升 PG”之间；检查失败时 Patroni 取消晋升并释放锁。脚本设 12 秒上限，异常不会被当作成功。

## 为什么去掉坏备库的等待仍然安全

```text
正常：S1 ──必须收到 WAL──► S2、S3 ──► CDC 可发送
                         两台都能被允许接管

S2 离线：
  1. etcd 持久记录：只允许 S3 接管 CDC
  2. S1 的 CDC 等待集合改为只等 S3
  3. 事件继续投递；S2 即使回来，也不能绕过晋升检查

S2 恢复：
  1. 核对其物理复制来自当前主库、永久同步槽有效
  2. 先把 S2 加回 WAL 等待集合
  3. 记录主库 flush LSN，确认候选已 replay 到该位置
  4. etcd 再允许 S2 参与接管
```

关键不是“检测端口通就放行”。安全顺序为：**扩展 WAL 屏障 → 等待新增候选追上准入位点 → 提交接管名单 → 缩小屏障**。进程在任意两步之间退出，最多多等待，不应出现“仍可晋升却已不再保有已发送 WAL”的节点。

etcd 键 `/service/crawl-pg/cdc_guard` 保存 owner、allowed、floor、system_id、generation。读取使用线性一致事务；写入 CAS 同时比较 leader 名称、leader revision 和旧 policy revision。只有当期主库能写，新主接管后旧主不能覆盖其名单。记录不绑定旧主租约，故旧主离线时准入证据仍可用。

晋升检查核对当前 leader 锁属于本机、名单允许本机、数据库 system identifier 一致、本地槽 failover/synced/永久/未失效且保留 WAL、本地 replay 不落后于准入位点。检查结束再次核对 DCS；在真正晋升之前，将本机 CDC 屏障重置为**另外两台**。新主随后按同样的顺序建立自己的名单，排除失联旧主，恢复投递。

运行中的等待集合通过 `ALTER SYSTEM` 设置并确认 reload 生效，覆盖 Patroni 本地配置里的保守双备库默认值。`SHOW synchronized_standby_slots` 才是当前实际值。不能只看 Patroni YAML 就认为仍固定等待两台。

没有可用候选、DCS 无多数派、必要 WAL 已丢失或槽失效时，保持等待/拒绝晋升并报告异常，不自动清空槽、跳过消息或放宽为零备库。该保护有意优先保数据，不承诺任意故障都可继续写。

## 旧主返回时的自动修复

备用节点发现精确同名槽为 inactive、failover、非 synced 且未失效时：

1. 检查当前主库政策已经排除自己，且确实正在从该主库物理复制。
2. 检查主库对应槽有效、confirmed LSN 不小于本地旧槽，并再次核对 DCS 未改变。
3. 在本机文件锁保护下，只删除本机只读 PG 上符合这些条件的旧槽。该锁也用于 pre_promote，避免本机修复与晋升检查并行。
4. PG17 原生同步重建备用槽；只有永久槽有效、复制追上准入位点，主库 guard 才重新纳入候选名单。

若原生初始化等待目录/WAL 推进，主库最多每 60 秒执行一次 CHECKPOINT 与隔离 outbox 恢复标记提交，走真实解码，不强行修改位点；只在有返回节点的槽缺失、临时或未同步时触发。这是当前运维探针 schema 的恢复辅助，正式业务 outbox 上线前必须重新设计受限心跳/恢复策略，不能直接把探针写入业务。

不存在自动删除主库活动槽的分支。未知/失效槽、上游进度落后或政策变化会停止修复。旧的 `repair-demoted-slot.py` 保留作历史维护工具，不与后台 guard 并发操作同一槽；若确需人工处理，先维护隔离并暂停该节点 guard，保留晋升 hook 与主库屏障。

## 实测结果及测试范围

`verify-ha-guard.py --execute` 在现有三节点验证：

| 场景 | 结果 | 本次观察耗时 |
|---|---|---|
| 普通备库 S2 PostgreSQL/Patroni 服务停止 | 自动排除 S2，S1 只等 S3，事件继续收到 | 3.27 秒 |
| 同步备库 S3 服务停止 | Patroni 重新选择同步备库，S1 只等 S2，事件继续收到 | 8.64 秒 |
| S1 到三个 etcd 的连接被阻断 | S1 降为只读，S2 自动接管并只等 S3，隔离期间事件收到 | 40.56 秒 |
| 停服/失联节点恢复 | 有效原生槽及复制进度通过后自动重新准入 | 通过 |
| 首次创建槽的旧主返回，留下非 synced 本地槽 | 独立探针复现，已安装修复路径删除本地旧槽，原生重建永久 synced 槽 | 通过 |

32 条确认提交事件全部收到，本次未观察到重复。计时从故障注入开始到阶段检查/事件收到结束，不是压力测试或生产 RTO 承诺；未做整机断电、云宿主机冻结或双节点同时失效演练。

旧槽冲突使用独立 `crawl_cdc_repair_probe_<随机后缀>`，没有删改正在服务的 Debezium 槽或 Kafka offset。旧主暂设 nofailover 以保持探针隔离，通过已安装程序 `--once --repair-probe-slot <name>` 执行与常驻服务相同的 standby 修复路径；验证器用该探针槽的真实解码和提交推动原生同步。这个测试证明修复路径实际可运行，但不能误称为“对活跃 Debezium 槽制造破坏后完整无人干预恢复”。常驻服务自动检测/准入，以及主库自动接管由真实运行服务验证。

13 项 guard 安全测试（含多个拒绝分支）及原维护脚本 5 项测试通过。证据：`ops/checks/2026-09-21-cdc-guard-failover.json`、`2026-09-21-cdc-guard-health.json`，部署记录 `2026-09-21-cdc-guard-deploy.json`。

## 运维、更新与回滚

```bash
python3 ops/cdc/check-health.py
# 在各 S 节点：
sudo systemctl status crawl-cdc-guard
sudo journalctl -u crawl-cdc-guard --since '10 minutes ago'
sudo cat /var/lib/crawl-cdc-guard/status.json
```

正常主库 READY、备库 STANDBY_READY。状态时间过期 15 秒或异常、主库名单与实际屏障不一致会触发只读检查失败。当前仍是按需检查，下一阶段接集中监控和告警。

初次部署用 `deploy-ha-guard.py --execute`，先下发全部程序、建立保守初始名单，再安装所有晋升 hook，最后启动服务；部署前原配置保存于忽略目录 `secrets/cdc/pre-guard/`。不要在故障期间重跑初始化部署。更新需先通过测试，将程序以 root-owned 0755 原子安装，再逐台重启 guard；配置更新与多节点协议变更必须考虑不同版本共存。

回滚需三节点健康且槽已同步：先停止三台 guard，保持晋升 hook；将**三台** `synchronized_standby_slots` 显式改回各自另外两台并确认生效，确认不存在缺失副本或保留 WAL 缺口，再去掉晋升 hook。不能先停用 hook、后缩减/清理政策，也不能简单删除 `postgresql.auto.conf`。该回滚恢复原来的保守可用性边界，不恢复旧数据。

节点加入/删除、更名、复制拓扑变化、DCS 灾难恢复或新增业务逻辑槽前，应暂停自动准入并更新这套白名单和恢复流程。当前脚本固定三节点及一个已验证槽，不是适用于任意 PostgreSQL 拓扑的通用 HA 产品。灾备恢复后的 policy/PG/WAL 必须重新核对，不能把带旧政策的 etcd 快照直接当成 CDC 恢复完成。

Kafka/PG 传输加密、连续告警、槽超限重放、联合灾备、真实业务 Consumer 幂等及其他外围 HA 遗留不在本次修复范围内。

实现核对依据：现场 Patroni 4.1.5 的 [`_pre_promote` 调用点](https://github.com/patroni/patroni/blob/v4.1.5/patroni/postgresql/__init__.py)、[PostgreSQL 17 复制配置](https://www.postgresql.org/docs/17/runtime-config-replication.html) 与原生 `slot.c`/`slotfuncs.c` 的同步槽限制。动态名单协议属于本仓库新增运维逻辑，不把它描述成 Patroni 原生自带功能。
