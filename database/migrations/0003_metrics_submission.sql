CREATE SCHEMA control;
CREATE SCHEMA ingestion;
CREATE DOMAIN platform.stable_id AS text CHECK (length(VALUE) BETWEEN 1 AND 256 AND VALUE ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]*$');
-- Unconstrained numeric plus CHECK avoids numeric(20,0) silently rounding fractional input.
CREATE DOMAIN platform.counter20 AS numeric CHECK (VALUE >= 0 AND VALUE <= 99999999999999999999 AND scale(VALUE) = 0);
CREATE DOMAIN platform.sha256 AS text CHECK (VALUE ~ '^[0-9a-f]{64}$');

CREATE TABLE control.plans (
  plan_id platform.stable_id PRIMARY KEY,
  channel_id text NOT NULL REFERENCES crawler.channels(channel_id),
  intent_key platform.stable_id NOT NULL UNIQUE,
  intent_hash platform.sha256 NOT NULL,
  kind text NOT NULL CHECK (kind = 'INCREMENTAL'),
  lifecycle_state text NOT NULL DEFAULT 'active' CHECK (lifecycle_state IN ('active','cancelled')),
  policy_version platform.stable_id NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(plan_id, channel_id)
);
CREATE TABLE control.channel_coordination (
  channel_id text PRIMARY KEY REFERENCES crawler.channels(channel_id),
  active_plan_id platform.stable_id,
  FOREIGN KEY(active_plan_id,channel_id) REFERENCES control.plans(plan_id,channel_id)
);
CREATE TABLE control.execution_authorizations (
  scope platform.stable_id PRIMARY KEY,
  plan_id platform.stable_id NOT NULL,
  channel_id text NOT NULL,
  execution_epoch platform.counter20 NOT NULL,
  holder_id platform.stable_id NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','revoked')),
  expires_at timestamptz NOT NULL,
  FOREIGN KEY(plan_id,channel_id) REFERENCES control.plans(plan_id,channel_id)
);
CREATE TABLE control.domain_obligations (
  plan_id platform.stable_id NOT NULL,
  channel_id text NOT NULL,
  domain text NOT NULL CHECK(domain = 'VIDEO_METRICS'),
  generation platform.counter20 NOT NULL,
  input_revision platform.counter20 NOT NULL,
  input_hash platform.sha256 NOT NULL,
  target_hash platform.sha256 NOT NULL,
  policy_version platform.stable_id NOT NULL,
  work_sealed boolean NOT NULL CHECK(work_sealed),
  PRIMARY KEY(plan_id,domain,generation),
  UNIQUE(plan_id,channel_id,domain,generation,input_revision,input_hash,target_hash,policy_version),
  FOREIGN KEY(plan_id,channel_id) REFERENCES control.plans(plan_id,channel_id)
);
CREATE TABLE ingestion.logical_batches (
  logical_batch_key platform.stable_id PRIMARY KEY,
  plan_id platform.stable_id NOT NULL,
  channel_id text NOT NULL,
  domain text NOT NULL,
  generation platform.counter20 NOT NULL,
  input_revision platform.counter20 NOT NULL,
  input_hash platform.sha256 NOT NULL,
  target_hash platform.sha256 NOT NULL,
  policy_version platform.stable_id NOT NULL,
  -- This slice has one bounded batch per obligation; multi-batch proof/settlement follows later.
  UNIQUE(plan_id,domain,generation),
  UNIQUE(logical_batch_key,plan_id,channel_id),
  FOREIGN KEY(plan_id,channel_id,domain,generation,input_revision,input_hash,target_hash,policy_version)
    REFERENCES control.domain_obligations(plan_id,channel_id,domain,generation,input_revision,input_hash,target_hash,policy_version)
);
CREATE TABLE control.domain_items (
  logical_batch_key platform.stable_id NOT NULL REFERENCES ingestion.logical_batches(logical_batch_key),
  source_content_id platform.stable_id NOT NULL,
  content_type text NOT NULL CHECK(content_type IN ('video','short','live')),
  ordinal integer NOT NULL CHECK(ordinal >= 0 AND ordinal < 100),
  PRIMARY KEY(logical_batch_key,source_content_id),
  UNIQUE(logical_batch_key,ordinal)
);
CREATE TABLE ingestion.submissions (
  submission_id platform.stable_id PRIMARY KEY,
  logical_batch_key platform.stable_id NOT NULL,
  plan_id platform.stable_id NOT NULL,
  channel_id text NOT NULL,
  original_scope platform.stable_id NOT NULL,
  original_execution_epoch platform.counter20 NOT NULL,
  original_holder_id platform.stable_id NOT NULL,
  payload_schema_id text NOT NULL CHECK(payload_schema_id = 'content.metrics.batch/1-draft.1'),
  hash_profile text NOT NULL CHECK(hash_profile = 'jcs-sha256-v1'),
  content_sha256 platform.sha256 NOT NULL,
  canonical_payload bytea NOT NULL CHECK(octet_length(canonical_payload) BETWEEN 1 AND 1048576),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(submission_id,logical_batch_key,plan_id,channel_id,content_sha256),
  FOREIGN KEY(logical_batch_key,plan_id,channel_id) REFERENCES ingestion.logical_batches(logical_batch_key,plan_id,channel_id)
);
CREATE TABLE ingestion.receipts (
  receipt_id platform.stable_id PRIMARY KEY,
  submission_id platform.stable_id NOT NULL,
  logical_batch_key platform.stable_id NOT NULL,
  plan_id platform.stable_id NOT NULL,
  channel_id text NOT NULL,
  content_sha256 platform.sha256 NOT NULL,
  status text NOT NULL CHECK(status = 'APPLIED'),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(submission_id,status),
  UNIQUE(logical_batch_key),
  UNIQUE(receipt_id,logical_batch_key,submission_id),
  FOREIGN KEY(submission_id,logical_batch_key,plan_id,channel_id,content_sha256)
    REFERENCES ingestion.submissions(submission_id,logical_batch_key,plan_id,channel_id,content_sha256)
);
CREATE TABLE ingestion.batch_checkpoints (
  logical_batch_key platform.stable_id PRIMARY KEY,
  submission_id platform.stable_id NOT NULL,
  receipt_id platform.stable_id NOT NULL,
  item_count integer NOT NULL CHECK(item_count BETWEEN 1 AND 100),
  item_outcomes jsonb NOT NULL CHECK(jsonb_typeof(item_outcomes) = 'array' AND jsonb_array_length(item_outcomes) = item_count),
  FOREIGN KEY(receipt_id,logical_batch_key,submission_id)
    REFERENCES ingestion.receipts(receipt_id,logical_batch_key,submission_id)
);

CREATE FUNCTION platform.reject_record_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Frozen record cannot be updated' USING ERRCODE = '23514';
END
$$;
CREATE TRIGGER frozen_obligation BEFORE UPDATE ON control.domain_obligations FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE TRIGGER frozen_batch BEFORE UPDATE ON ingestion.logical_batches FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE TRIGGER frozen_item BEFORE UPDATE ON control.domain_items FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE TRIGGER immutable_submission BEFORE UPDATE ON ingestion.submissions FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE TRIGGER immutable_receipt BEFORE UPDATE ON ingestion.receipts FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();
CREATE TRIGGER immutable_checkpoint BEFORE UPDATE ON ingestion.batch_checkpoints FOR EACH ROW EXECUTE FUNCTION platform.reject_record_update();

COMMENT ON TABLE ingestion.batch_checkpoints IS 'Atomic metrics batch apply evidence only; not Domain success, Plan settlement, Feature completion or Business delivery.';
