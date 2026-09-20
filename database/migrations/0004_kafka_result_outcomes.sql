-- Offset advancement is permitted only after a durable applied/quarantined record.
CREATE TABLE ingestion.kafka_record_outcomes (
  stream_id platform.stable_id NOT NULL,
  topic platform.stable_id NOT NULL,
  partition_id integer NOT NULL CHECK(partition_id >= 0),
  record_offset bigint NOT NULL CHECK(record_offset >= 0),
  record_sha256 platform.sha256 NOT NULL,
  disposition text NOT NULL CHECK(disposition IN ('APPLIED','QUARANTINED')),
  receipt_id platform.stable_id REFERENCES ingestion.receipts(receipt_id),
  error_code text,
  key_bytes bytea,
  raw_value bytea,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(stream_id,topic,partition_id,record_offset),
  CHECK (CASE WHEN disposition='APPLIED' THEN receipt_id IS NOT NULL AND error_code IS NULL AND raw_value IS NULL
    ELSE receipt_id IS NULL AND error_code IS NOT NULL END),
  CHECK(octet_length(key_bytes) <= 4096),
  CHECK(octet_length(raw_value) <= 2097152)
);
CREATE TRIGGER immutable_kafka_outcome BEFORE UPDATE ON ingestion.kafka_record_outcomes
  FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE INDEX kafka_quarantine_time_idx ON ingestion.kafka_record_outcomes(recorded_at)
  WHERE disposition='QUARANTINED';
COMMENT ON TABLE ingestion.kafka_record_outcomes IS
  'Kafka record disposition, not a Submission RECEIVED or negative receipt. stream_id must change if topic is recreated. Quarantine retains raw value for explicit recovery.';
