# SeaweedFS 验证部署

日期：2026-09-18

## 目标拓扑

```text
S1/S2/S3  SeaweedFS Master :9333 / gRPC :19333
S1/S2/S3  SeaweedFS Volume :8080 / gRPC :18080
S3         SeaweedFS Filer :8888 + S3 Gateway :8333（验证入口）
```

SeaweedFS 只承接大 Raw、恢复对象和超大 Submission；普通结构化 Delta 仍然直接进入 Data Ingestor → PostgreSQL。

## 已完成

- 三台节点已安装 SeaweedFS 4.47。
- Master、Volume、Filer、S3 的 systemd 单元和固定数据目录已写入安装脚本。
- 修正了 `/srv/crawlsystem` 的 `seaweedfs` 用户属组和遍历权限。

## 当前状态

服务已暂时停止。SeaweedFS Master gRPC 和 Volume 端口尚未在轻量云防火墙放通，三节点无法完成 Master/Volume 互联。

放通以下内网入站端口后再启动：

| 端口 | 用途 |
|---:|---|
| 9333/TCP | Master HTTP/Raft |
| 19333/TCP | Master gRPC |
| 8080/TCP | Volume HTTP |
| 18080/TCP | Volume gRPC |
| 8888/TCP | Filer HTTP |
| 8333/TCP | S3 Gateway |

来源统一为 `10.4.4.0/22`，应用到 S1/S2/S3。
