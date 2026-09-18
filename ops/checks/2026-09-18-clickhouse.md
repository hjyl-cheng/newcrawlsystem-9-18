# ClickHouse 验证部署

日期：2026-09-18

## 已完成

- S3（10.4.4.5）安装 ClickHouse 26.8.6.5，systemd 服务 active。
- 修复安装停在密码提示的问题：使用 `sudo env DEBIAN_FRONTEND=noninteractive`。
- 监听 127.0.0.1 和 10.4.4.5 的 HTTP 8123、Native 9000。
- 建立 `crawler_analytics` 数据库与 `crawler_validation` 验证账号。密码由本地 `secrets/clickhouse-validation.env` 提供，服务器配置保存 SHA-256，不提交凭据。
- 默认账号仅允许回环访问；验证账号允许回环与 10.4.4.0/22，数据库范围为 crawler_analytics。
- 验证账号完成 MergeTree 建表、插入、服务重启、读取校验和删除测试表。
- 本机 Native 客户端查询版本成功。

## 内网连接验收（已通过）

用户更新 S3（公网实例地址 43.172.80.48）的轻量云防火墙后，重新实测：

- A1 → 10.4.4.5:8123 TCP 连通。
- A1 → 10.4.4.5:9000 TCP 连通。
- A1 使用 `crawler_validation` 经 HTTP 认证查询，返回版本 26.8.6.5 和正确账号。
- A1 经 HTTP 完成临时 MergeTree 表创建、插入、读取校验及删除；测试表已清理。

当前内网入站规则：

| 协议 | 端口 | 来源 | 用途 |
|---|---|---|---|
| TCP | 8123 | 10.4.4.0/22 | HTTP 分析接口 |
| TCP | 9000 | 10.4.4.0/22 | Native 客户端 |

本次跨节点读写使用 HTTP 8123。9000 已验证 TCP 连通，本机 Native 查询此前通过；跨节点 Native 认证查询未单独执行。

## 当前边界

单节点验证部署，数据位于 `/var/lib/clickhouse` 的系统盘，尚未接入 analytics outbox、业务表、备份和生产容量验收。ClickHouse 不参与 Crawler PG 的权威事务。

## 重复部署

将当前仓库的安装脚本及本地凭据文件复制到 S3，再执行：

```bash
CRAWL_CH_SECRET_FILE=/tmp/clickhouse-validation.env bash /tmp/install-clickhouse-validation.sh
```

脚本固定软件包版本，保留既有数据；本地默认账号用于建库，应用验证使用 crawler_validation。
