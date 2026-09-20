-- Local storage integrity hash, not a wire/JCS or Publication hash.
ALTER TABLE crawler.contents
  ADD COLUMN comments_first_page_hash text,
  ADD COLUMN comments_first_page_hash_profile text,
  ADD COLUMN comments_first_page_observed_at timestamptz,
  ADD COLUMN comments_page_checked_at timestamptz;

CREATE FUNCTION crawler.derive_comment_page_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.comments_first_page IS NULL THEN
    NEW.comments_first_page_hash := NULL;
    NEW.comments_first_page_hash_profile := NULL;
    NEW.comments_first_page_observed_at := NULL;
  ELSIF crawler.valid_comment_page(NEW.comments_first_page) THEN
    IF TG_OP = 'INSERT' OR NEW.comments_first_page IS DISTINCT FROM OLD.comments_first_page THEN
      NEW.comments_first_page_hash := encode(sha256(convert_to(NEW.comments_first_page::text, 'UTF8')), 'hex');
      NEW.comments_first_page_hash_profile := 'pg17-jsonb-text-sha256-v1';
      NEW.comments_first_page_observed_at := (NEW.comments_first_page->>'collected_at')::timestamptz;
    ELSE
      NEW.comments_first_page_hash := OLD.comments_first_page_hash;
      NEW.comments_first_page_hash_profile := OLD.comments_first_page_hash_profile;
      NEW.comments_first_page_observed_at := OLD.comments_first_page_observed_at;
    END IF;
  END IF;
  RETURN NEW;
END
$$;

-- Backfill existing pages before installing metadata protection; no invented page timestamp.
UPDATE crawler.contents SET
  comments_first_page_hash = encode(sha256(convert_to(comments_first_page::text, 'UTF8')), 'hex'),
  comments_first_page_hash_profile = 'pg17-jsonb-text-sha256-v1',
  comments_first_page_observed_at = (comments_first_page->>'collected_at')::timestamptz,
  comments_page_checked_at = (comments_first_page->>'collected_at')::timestamptz
WHERE comments_first_page IS NOT NULL;

ALTER TABLE crawler.contents ADD CONSTRAINT contents_page_evidence CHECK (
  CASE WHEN comments_first_page IS NULL THEN
    comments_first_page_hash IS NULL AND comments_first_page_hash_profile IS NULL AND comments_first_page_observed_at IS NULL
  ELSE (comments_first_page_hash ~ '^[0-9a-f]{64}$' AND
    comments_first_page_hash_profile = 'pg17-jsonb-text-sha256-v1' AND
    comments_first_page_observed_at = (comments_first_page->>'collected_at')::timestamptz) IS TRUE END);

CREATE TRIGGER contents_page_evidence BEFORE INSERT OR UPDATE OF
  comments_first_page, comments_first_page_hash, comments_first_page_hash_profile, comments_first_page_observed_at
  ON crawler.contents FOR EACH ROW EXECUTE FUNCTION crawler.derive_comment_page_evidence();

COMMENT ON COLUMN crawler.contents.comments_first_page_hash IS
  'SHA-256 of UTF-8 PostgreSQL 17 jsonb::text; local integrity only, never a wire JCS/Publication hash.';
COMMENT ON COLUMN crawler.contents.comments_page_checked_at IS
  'Latest valid page observation accepted by Store; separate from retained first-page collected_at.';
