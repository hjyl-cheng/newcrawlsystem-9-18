-- Infrastructure-owned schema in the existing single logical crawler database.
-- Role creation/secret rotation is performed separately over protected stdin.
CREATE SCHEMA IF NOT EXISTS object_metadata AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA object_metadata FROM PUBLIC;
CREATE TABLE IF NOT EXISTS object_metadata.filemeta (
    dirhash BIGINT NOT NULL,
    name VARCHAR(65535) COLLATE "C" NOT NULL,
    directory VARCHAR(65535) NOT NULL,
    meta BYTEA,
    PRIMARY KEY (dirhash, name)
);
GRANT CONNECT ON DATABASE crawler TO seaweedfs_filer;
GRANT USAGE ON SCHEMA object_metadata TO seaweedfs_filer;
GRANT SELECT, INSERT, UPDATE, DELETE ON object_metadata.filemeta TO seaweedfs_filer;
ALTER ROLE seaweedfs_filer IN DATABASE crawler SET search_path = object_metadata, pg_catalog;
ALTER ROLE seaweedfs_filer IN DATABASE crawler SET statement_timeout = '30s';
ALTER ROLE seaweedfs_filer IN DATABASE crawler SET idle_in_transaction_session_timeout = '30s';
