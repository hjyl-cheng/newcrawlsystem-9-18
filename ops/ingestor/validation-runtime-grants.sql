-- Run as migration owner, only in crawler_validation_ingestor.
DO $$ BEGIN
  IF current_database() <> 'crawler_validation_ingestor' THEN
    RAISE EXCEPTION 'Dedicated validation database required';
  END IF;
END $$;
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA platform,crawler,control,ingestion TO crawler_ingestor_validation;
GRANT SELECT ON platform.schema_migrations TO crawler_ingestor_validation;
GRANT SELECT ON control.plans,control.channel_coordination,control.execution_authorizations,
  control.domain_items,ingestion.logical_batches TO crawler_ingestor_validation;
-- PostgreSQL requires an UPDATE privilege for SELECT FOR UPDATE. Allow row locks,
-- but deny actual UPDATE statements by this runtime role (including zero-row updates).
CREATE OR REPLACE FUNCTION platform.deny_ingestor_control_update() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
  IF current_user='crawler_ingestor_validation' THEN
    RAISE EXCEPTION 'Runtime consumer cannot mutate control authority' USING ERRCODE='42501';
  END IF;
  RETURN NULL;
END $$;
CREATE OR REPLACE TRIGGER deny_ingestor_control_update BEFORE UPDATE ON control.plans
  FOR EACH STATEMENT EXECUTE FUNCTION platform.deny_ingestor_control_update();
CREATE OR REPLACE TRIGGER deny_ingestor_control_update BEFORE UPDATE ON control.channel_coordination
  FOR EACH STATEMENT EXECUTE FUNCTION platform.deny_ingestor_control_update();
CREATE OR REPLACE TRIGGER deny_ingestor_control_update BEFORE UPDATE ON control.execution_authorizations
  FOR EACH STATEMENT EXECUTE FUNCTION platform.deny_ingestor_control_update();
CREATE OR REPLACE TRIGGER deny_ingestor_control_update BEFORE UPDATE ON ingestion.logical_batches
  FOR EACH STATEMENT EXECUTE FUNCTION platform.deny_ingestor_control_update();
GRANT UPDATE(plan_id) ON control.plans TO crawler_ingestor_validation;
GRANT UPDATE(channel_id) ON control.channel_coordination TO crawler_ingestor_validation;
GRANT UPDATE(scope) ON control.execution_authorizations TO crawler_ingestor_validation;
GRANT UPDATE(logical_batch_key) ON ingestion.logical_batches TO crawler_ingestor_validation;
GRANT SELECT,INSERT ON ingestion.submissions,ingestion.receipts,ingestion.batch_checkpoints,
  ingestion.kafka_record_outcomes TO crawler_ingestor_validation;
GRANT SELECT ON crawler.contents TO crawler_ingestor_validation;
GRANT INSERT(channel_id,platform,resource_kind,source_content_id,content_type,first_seen_at,last_seen_at)
  ON crawler.contents TO crawler_ingestor_validation;
GRANT UPDATE(view_count,view_count_status,view_count_source,view_count_observed_at,view_count_text,
  like_count,like_count_status,like_count_source,like_count_observed_at,
  comment_count,comment_count_status,comment_count_source,comment_count_observed_at,comments_disabled,
  comments_first_page,comments_page_checked_at,first_seen_at,last_seen_at)
  ON crawler.contents TO crawler_ingestor_validation;
GRANT EXECUTE ON FUNCTION crawler.valid_comment_page(jsonb) TO crawler_ingestor_validation;
COMMIT;
