# Grok 监控改动评审（2026-09-22）

## 结论

本批改动可以保留并继续验证，结论为 **条件通过**：官方 JSON Exporter、Blackbox Exporter、postgres_exporter 和 node_exporter systemd collector 已经运行，Prometheus 目标可抓取，NetworkPolicy 没有因为新增 exporter 而开放公网入口。

这批改动还不能称为“已替代旧监控”。旧 `infra-exporter`、旧 textfile 探针和旧告警仍然是当前判断来源，官方 exporter 只做并行对照，这个取舍是正确的。

## 已核对事实

- Git 当前为 `14cd9ea`，工作区在评审前干净，远端 `main` 与本地一致。
- A1/A2/A3 均为 `Ready`，Kubernetes `v1.37.0`，containerd `2.4.0`。
- Argo CD `3.5.3`；8 个 validation Application 均为 `Synced/Healthy`。
- JSON Exporter `v0.8.0`、Blackbox Exporter `v0.28.0`、postgres_exporter `v0.20.1` 均为固定 digest，副本均为 `Running/Ready`。
- Prometheus 当前所有目标为 `up`，Blackbox 实测 Connect status、Ingestor live、Ingestor ready 均返回 HTTP 200。
- PostgreSQL exporter 使用 `sslmode=verify-full` 和专用只读角色 `crawl_pg_exporter`；`pg_up=1`，角色限制为非超级用户、不可建库/建角色、连接上限 4，并且 HBA 来源限定为三台 A 节点。
- 当前配置使用 Prometheus static scrape，没有安装 Prometheus Operator，因此 `kubectl get servicemonitor` 找不到资源是预期结果，不应在文档中称为使用 ServiceMonitor。

## 发现的问题

### 1. JSON Exporter 指标名与直觉不同，但配置行为是正确的

JSON Exporter 实际输出的是：

```text
crawl_json_connect_tasks_present{task_id="0"} 1
crawl_json_connect_tasks_failed_present{task_id="0"} 1
```

因此查询 `crawl_json_connect_tasks` 或 `crawl_json_connect_tasks_failed` 会得到空结果。`up=1` 只表示 exporter HTTP 接口可抓取，不能表示 Connector task 正常。后续面板和规则必须使用带 `_present` 后缀的指标，或者由 Prometheus recording rule 显式归一化；不能把空查询当成“没有任务”或“任务正常”。

### 2. CDC 当前确实不健康，且不是 exporter 引起的

直接查询 Connect status 得到：Connector 为 `RUNNING`，唯一 task `id=0` 为 `FAILED`，错误为无法取得有效 replication slot。PostgreSQL exporter 和旧探针同时显示：

- `crawl_cdc_validation` 的 `wal_status="lost"`；
- 槽不活跃、`valid=0`；
- 估算滞留 WAL 约 2.6GB；
- `CdcSlotInvalid`、`CdcGuardNotReady`、`CdcBarrierMismatch` 和 WAL 保留告警正在触发。

这说明新增 exporter 做到了真实暴露故障，但当前外围验收不能标记为全绿。不得直接删除槽、清空 offset 或关闭告警来消除现象；应按 CDC Guard 的恢复流程确认候选主库、LSN 和幂等边界后，再修复 Connector/槽位并重新验证。

### 3. PostgreSQL exporter 适合做对照，暂不适合直接接管旧告警

它能读取主库复制状态、槽位和 PostgreSQL 设置，但当前通过 validation 的 `postgres-rw` Service 访问，Service 有两个 PostgreSQL entry endpoint。此次验证证明连接和权限有效，尚未证明整机切主时 exporter 能稳定跟随新主，也没有把旧 `crawl_pg_*` 指标逐项证明为等价。因此保留旧告警是正确的，切换前还需要做一次受控主备切换和逐指标对比。

### 4. 生成源和提交产物存在双份，需要保持生成一致

监控配置由 `ops/monitoring/render.py` 生成，同时提交到 `deploy/base/monitoring`。本次 `git diff --check` 和 `kubectl kustomize deploy/overlays/validation/monitoring` 均通过，但以后修改必须先改生成源、重新 render，再提交产物，避免 Argo 使用的清单与源文件漂移。

## 处理决定

1. 保留官方 exporter、旧探针和旧告警并行运行，不删除 `infra-exporter`。
2. 将 JSON Exporter 的 `_present` 指标纳入后续面板/规则评审；在等价性验证前不接管 `ConnectNotRunning` 告警。
3. 将 CDC task FAILED、无效槽和 WAL 滞留列为当前最高优先级运维问题；外围组件评审通过不等于 CDC 链路通过。
4. PostgreSQL exporter 先完成受控切主、权限复核和指标对照，再决定是否替换旧 PG 采集器。
5. 在以上两项完成前，不继续删除旧探针，也不把当前监控状态称为生产级全链路通过。

## 下一步

- 先按现有 CDC Guard 流程恢复 `crawl_cdc_validation`，确认 Connector task 回到 `RUNNING` 且槽 `wal_status` 不再为 `lost`。
- 恢复后重新查询 JSON Exporter 的 `_present` 指标、Blackbox 指标和旧探针，保存前后对照证据。
- 对 PostgreSQL exporter 做一次不破坏数据的主备切换验证，确认 `pg_up`、复制和槽位指标随主库变化。
- 只有等价性和故障恢复验证完成后，才讨论切换告警来源；外围继续优先采用官方成熟组件，不新增常驻自研 exporter。

