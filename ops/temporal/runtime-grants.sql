\set ON_ERROR_STOP on
-- Run as postgres after the official schema job. No grants on crawler/Business.
ALTER ROLE temporal_validation_runtime CONNECTION LIMIT 64;
\connect temporal_validation
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO temporal_validation_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO temporal_validation_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO temporal_validation_runtime;
REVOKE INSERT, UPDATE, DELETE ON public.schema_version, public.schema_update_history FROM temporal_validation_runtime;
COMMIT;
\connect temporal_visibility_validation
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO temporal_validation_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO temporal_validation_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO temporal_validation_runtime;
REVOKE INSERT, UPDATE, DELETE ON public.schema_version, public.schema_update_history FROM temporal_validation_runtime;
COMMIT;
