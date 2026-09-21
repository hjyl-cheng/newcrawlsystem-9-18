-- Infrastructure probe only: not the production publication.outbox schema.
CREATE SCHEMA IF NOT EXISTS cdc_validation AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA cdc_validation FROM PUBLIC;
CREATE TABLE IF NOT EXISTS cdc_validation.outbox (
 id uuid PRIMARY KEY,
 aggregatetype text NOT NULL,
 aggregateid text NOT NULL,
 type text NOT NULL,
 payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cdc_validation.excluded_noise (id bigint PRIMARY KEY, value text NOT NULL);
GRANT CONNECT ON DATABASE crawler TO crawl_cdc_validation;
GRANT USAGE ON SCHEMA cdc_validation TO crawl_cdc_validation;
GRANT SELECT ON cdc_validation.outbox TO crawl_cdc_validation;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_publication WHERE pubname='crawl_cdc_validation') THEN
  CREATE PUBLICATION crawl_cdc_validation FOR TABLE cdc_validation.outbox;
 END IF;
END $$;
