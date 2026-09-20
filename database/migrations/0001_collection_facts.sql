-- First facts slice only. Plans, authorization, receipts and publication follow separately.
CREATE SCHEMA crawler;

-- CHECK must return false (not NULL) for malformed JSON. CASE prevents unsafe casts.
CREATE FUNCTION crawler.valid_comment_page(page jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN page IS NULL THEN true
    WHEN jsonb_typeof(page) IS DISTINCT FROM 'object' THEN false
    WHEN page->'version' IS DISTINCT FROM '1'::jsonb THEN false
    WHEN (page->>'sort' IN ('TOP_COMMENTS', 'NEWEST_FIRST')) IS NOT TRUE THEN false
    WHEN jsonb_typeof(page->'comments') IS DISTINCT FROM 'array' THEN false
    WHEN jsonb_typeof(page->'returned_count') IS DISTINCT FROM 'number' THEN false
    WHEN (page->>'returned_count' ~ '^[0-9]+$') IS NOT TRUE THEN false
    WHEN jsonb_typeof(page->'collected_at') IS DISTINCT FROM 'string' THEN false
    WHEN (page->>'collected_at' ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$') IS NOT TRUE THEN false
    WHEN NOT pg_input_is_valid(page->>'collected_at', 'timestamp with time zone') THEN false
    ELSE (page->>'returned_count')::numeric = jsonb_array_length(page->'comments')
      AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(page->'comments') item WHERE jsonb_typeof(item) <> 'object')
  END
$$;

CREATE TABLE crawler.channels (
  channel_id text PRIMARY KEY CHECK (length(btrim(channel_id)) BETWEEN 1 AND 256),
  channel_url text NOT NULL CHECK (length(btrim(channel_url)) > 0),
  handle text, title text,
  lifecycle_status text NOT NULL DEFAULT 'active'
    CHECK (lifecycle_status IN ('active','dormant','paused','archived','rejected','removed')),
  reject_reason text,
  subscriber_count bigint CHECK (subscriber_count >= 0),
  subscriber_count_text text,
  subscriber_count_status text NOT NULL DEFAULT 'unresolved'
    CHECK (subscriber_count_status IN ('exact','estimated','unavailable','unresolved')),
  subscriber_count_source text, subscriber_count_observed_at timestamptz,
  total_view_count bigint CHECK (total_view_count >= 0), total_view_count_text text,
  total_view_count_status text NOT NULL DEFAULT 'unresolved'
    CHECK (total_view_count_status IN ('exact','estimated','unavailable','unresolved')),
  total_view_count_source text, total_view_count_observed_at timestamptz,
  total_video_count bigint CHECK (total_video_count >= 0), total_video_count_text text,
  total_video_count_status text NOT NULL DEFAULT 'unresolved'
    CHECK (total_video_count_status IN ('exact','estimated','unavailable','unresolved')),
  total_video_count_source text, total_video_count_observed_at timestamptz,
  country text, country_code text CHECK (country_code ~ '^[A-Z]{2}$'),
  country_canonical_name text, country_source text,
  avatar_url text, summary text,
  keywords text[] NOT NULL DEFAULT '{}', available_tabs text[] NOT NULL DEFAULT '{}',
  keywords_status text NOT NULL DEFAULT 'unresolved' CHECK (keywords_status IN ('observed','unresolved')),
  available_tabs_status text NOT NULL DEFAULT 'unresolved' CHECK (available_tabs_status IN ('observed','unresolved')),
  about_description text,
  description_status text NOT NULL DEFAULT 'unresolved' CHECK (description_status IN ('exact','empty','unresolved')),
  joined_at date, joined_date_text text,
  joined_at_precision text NOT NULL DEFAULT 'unknown' CHECK (joined_at_precision IN ('date_only','unknown')),
  external_links jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(external_links) = 'array'),
  external_links_status text NOT NULL DEFAULT 'unresolved' CHECK (external_links_status IN ('observed','unresolved')),
  rss_url text, vanity_channel_url text, is_family_safe boolean,
  is_verified boolean, is_verified_status text NOT NULL DEFAULT 'unknown',
  about_last_observed_at timestamptz, about_current_hash text,
  about_identity_last_observed_at timestamptz, about_identity_current_hash text,
  youtube_business_email_available boolean, youtube_business_email_observed_at timestamptz,
  removed_reason text, removed_at timestamptz, removed_source text, removed_evidence text,
  dormant_reason text, dormant_since timestamptz, dormant_recheck_day date,
  dormant_last_probe_at timestamptz, dormant_cycle integer NOT NULL DEFAULT 0 CHECK (dormant_cycle >= 0),
  evidence_json jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence_json) = 'object'),
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT channels_verified_state CHECK (
    (is_verified_status = 'verified' AND is_verified IS TRUE) OR
    (is_verified_status = 'not_verified' AND is_verified IS FALSE) OR
    (is_verified_status = 'unknown' AND is_verified IS NULL)),
  CONSTRAINT channels_email_observation CHECK (
    (youtube_business_email_available IS NULL) = (youtube_business_email_observed_at IS NULL)),
  CONSTRAINT channels_joined_precision CHECK ((joined_at IS NULL) = (joined_at_precision = 'unknown')),
  CONSTRAINT channels_metric_quality CHECK (
    ((subscriber_count IS NOT NULL) = (subscriber_count_status IN ('exact','estimated'))) AND
    ((total_view_count IS NOT NULL) = (total_view_count_status IN ('exact','estimated'))) AND
    ((total_video_count IS NOT NULL) = (total_video_count_status IN ('exact','estimated')))),
  CONSTRAINT channels_removed_evidence CHECK (lifecycle_status <> 'removed' OR
    (removed_reason IS NOT NULL AND removed_at IS NOT NULL AND removed_source IS NOT NULL)),
  CONSTRAINT channels_dormant_evidence CHECK ((CASE WHEN lifecycle_status = 'dormant' THEN
    dormant_reason IN ('no_published_content_within_90_days','uploads_empty') AND
    dormant_since IS NOT NULL AND dormant_recheck_day IS NOT NULL AND
    dormant_last_probe_at IS NOT NULL AND dormant_cycle > 0 AND reject_reason IS NULL
    ELSE dormant_reason IS NULL AND dormant_since IS NULL AND dormant_recheck_day IS NULL AND
      dormant_last_probe_at IS NULL AND dormant_cycle = 0 END) IS TRUE),
  CONSTRAINT channels_country_evidence CHECK (country_source IS DISTINCT FROM 'youtube_about' OR
    NULLIF(btrim(country), '') IS NOT NULL)
);

CREATE TABLE crawler.contents (
  content_key text PRIMARY KEY DEFAULT gen_random_uuid()::text CHECK (length(btrim(content_key)) BETWEEN 1 AND 256),
  channel_id text NOT NULL REFERENCES crawler.channels(channel_id) ON DELETE RESTRICT,
  platform text NOT NULL DEFAULT 'youtube' CHECK (platform = 'youtube'),
  resource_kind text NOT NULL CHECK (resource_kind IN ('youtube_video','youtube_post')),
  source_content_id text NOT NULL CHECK (length(btrim(source_content_id)) BETWEEN 1 AND 256),
  content_type text NOT NULL CHECK (content_type IN ('video','short','live','post')),
  content_type_source text,
  title text, url text, thumbnail_url text, description text,
  description_status text NOT NULL DEFAULT 'unresolved' CHECK (description_status IN ('exact','empty','unavailable','unresolved')),
  description_source text, hashtags text[] NOT NULL DEFAULT '{}', keywords text[] NOT NULL DEFAULT '{}',
  published_text_raw text, published_at timestamptz,
  published_at_status text NOT NULL DEFAULT 'unresolved' CHECK (published_at_status IN ('exact','relative','estimated','unavailable','unresolved')),
  published_at_source text,
  published_at_precision text NOT NULL DEFAULT 'unknown' CHECK (published_at_precision IN ('second','date_only','unknown')),
  length_text text, duration_seconds integer CHECK (duration_seconds >= 0),
  duration_status text NOT NULL DEFAULT 'unresolved' CHECK (duration_status IN ('exact','unavailable','unresolved')),
  duration_source text,
  view_count bigint CHECK (view_count >= 0), view_count_text text,
  view_count_status text NOT NULL DEFAULT 'unresolved' CHECK (view_count_status IN ('exact','estimated','unavailable','unresolved')),
  view_count_source text, view_count_observed_at timestamptz,
  like_count bigint CHECK (like_count >= 0),
  like_count_status text NOT NULL DEFAULT 'unresolved' CHECK (like_count_status IN ('exact','zero_from_empty','unavailable','unresolved')),
  like_count_source text, like_count_observed_at timestamptz,
  comment_count bigint CHECK (comment_count >= 0),
  comment_count_status text NOT NULL DEFAULT 'unresolved'
    CHECK (comment_count_status IN ('exact','zero_from_empty','zero_from_surface','zero_from_upcoming','disabled','unavailable','unresolved')),
  comment_count_source text, comment_count_observed_at timestamptz,
  comments_disabled boolean,
  comments_first_page jsonb CHECK (crawler.valid_comment_page(comments_first_page) IS TRUE),
  -- Unknown is NULL; a legacy reader's false fallback belongs in its adapter.
  is_members_only boolean,
  access_status text NOT NULL DEFAULT 'unknown'
    CHECK (access_status IN ('public','unlisted','members_only','private','unavailable','login_required','unknown')),
  access_status_source text,
  position integer CHECK (position >= 0), is_recent boolean NOT NULL DEFAULT false,
  live_scheduled_at timestamptz, live_started_at timestamptz, live_ended_at timestamptz,
  extractor_version text,
  first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
  last_enriched_at timestamptz, playlist_last_seen_at timestamptz,
  player_last_observed_at timestamptz, next_last_observed_at timestamptz,
  player_current_hash text, next_current_hash text,
  video_change_probability double precision CHECK (video_change_probability BETWEEN 0 AND 1),
  publication_item_hash text CHECK (publication_item_hash ~ '^sha256:[0-9a-f]{64}$'),
  evidence_json jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence_json) = 'object'),
  CONSTRAINT contents_source_identity UNIQUE (platform, resource_kind, source_content_id),
  CONSTRAINT contents_resource_type CHECK (
    (resource_kind = 'youtube_video' AND content_type IN ('video','short','live')) OR
    (resource_kind = 'youtube_post' AND content_type = 'post')),
  CONSTRAINT contents_metric_quality CHECK (
    ((view_count IS NOT NULL) = (view_count_status IN ('exact','estimated'))) AND
    ((like_count IS NOT NULL) = (like_count_status IN ('exact','zero_from_empty'))) AND
    ((comment_count IS NOT NULL) = (comment_count_status IN ('exact','zero_from_empty','zero_from_surface','zero_from_upcoming','disabled')))),
  CONSTRAINT contents_zero_evidence CHECK (
    (like_count_status <> 'zero_from_empty' OR like_count = 0) AND
    (comment_count_status NOT IN ('zero_from_empty','zero_from_surface','zero_from_upcoming','disabled') OR comment_count = 0)),
  CONSTRAINT contents_disabled_evidence CHECK ((CASE WHEN comments_disabled IS TRUE THEN
    comment_count = 0 AND comment_count_status = 'disabled'
    ELSE comment_count_status <> 'disabled' END) IS TRUE),
  CONSTRAINT contents_duration_quality CHECK ((duration_seconds IS NOT NULL) = (duration_status = 'exact')),
  CONSTRAINT contents_published_quality CHECK (
    ((published_at IS NOT NULL) = (published_at_status IN ('exact','relative','estimated'))) AND
    ((published_at IS NULL) = (published_at_precision = 'unknown'))),
  CONSTRAINT contents_membership_evidence CHECK ((CASE
    WHEN access_status = 'members_only' THEN is_members_only IS TRUE
    WHEN access_status IN ('public','unlisted') THEN is_members_only IS FALSE
    ELSE true END) IS TRUE)
);

CREATE INDEX contents_channel_published_idx ON crawler.contents(channel_id, published_at DESC)
  WHERE published_at IS NOT NULL;
CREATE INDEX contents_channel_seen_idx ON crawler.contents(channel_id, content_type, last_seen_at DESC);

-- Identity corrections require a separately reviewed repair, not ordinary facts updates.
CREATE FUNCTION crawler.guard_content_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.content_key, NEW.channel_id, NEW.platform, NEW.resource_kind, NEW.source_content_id)
    IS DISTINCT FROM ROW(OLD.content_key, OLD.channel_id, OLD.platform, OLD.resource_kind, OLD.source_content_id) THEN
    RAISE EXCEPTION 'Content identity is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER contents_identity_guard BEFORE UPDATE ON crawler.contents
  FOR EACH ROW EXECUTE FUNCTION crawler.guard_content_identity();

COMMENT ON COLUMN crawler.contents.comments_first_page IS
  'Retained first page including its original collected_at; current count has independent evidence. Store merge policy follows in a later migration/code slice.';
COMMENT ON COLUMN crawler.contents.evidence_json IS
  'Online field-observation/conflict evidence, not a dump of unbounded extractor responses.';
