CREATE DATABASE IF NOT EXISTS crawler_logs;
CREATE TABLE IF NOT EXISTS crawler_logs.events
(
    Timestamp DateTime64(3, 'UTC') CODEC(Delta, ZSTD(1)),
    Node LowCardinality(String),
    Service LowCardinality(String),
    Source LowCardinality(String),
    Namespace LowCardinality(String),
    Pod String CODEC(ZSTD(3)),
    Container LowCardinality(String),
    SeverityText LowCardinality(String),
    Body String CODEC(ZSTD(3)),
    EventId UUID,
    IngestedAt DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY formatDateTime(Timestamp, '%Y%m%d%H', 'UTC')
ORDER BY (Node, Service, Timestamp, EventId)
TTL toDateTime(Timestamp) + INTERVAL 3 DAY DELETE
SETTINGS index_granularity = 8192, merge_with_ttl_timeout = 3600, old_parts_lifetime = 60;
