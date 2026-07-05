-- 2026-05-22: versioned read model for the 信息 tab platform view.
--
-- Goal:
--   Keep the UI contract unchanged while moving hot reads away from the
--   large remote_poc.items live table:
--     - /api/feed/platforms reads section top cards + true counts.
--     - /api/feed/platforms/more reads source/group/category scoped pages.
--
-- The model is version-swapped. Builders insert a new version, fill all child
-- tables, then atomically point info_read_model_state at the completed version.
-- Readers never need to observe a partially built version.

set search_path = remote_poc, extensions, public;

CREATE TABLE IF NOT EXISTS remote_poc.info_read_model_versions (
  version_id uuid PRIMARY KEY,
  status text NOT NULL DEFAULT 'building'
    CHECK (status IN ('building', 'complete', 'error')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  max_fetched_at timestamptz,
  sample_limit integer NOT NULL DEFAULT 200,
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE TABLE IF NOT EXISTS remote_poc.info_read_model_state (
  key text PRIMARY KEY,
  active_version_id uuid NOT NULL REFERENCES remote_poc.info_read_model_versions(version_id) ON DELETE RESTRICT,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS remote_poc.info_scopes (
  version_id uuid NOT NULL REFERENCES remote_poc.info_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  platform text NOT NULL,
  dimension text NOT NULL CHECK (dimension IN ('all', 'source', 'group', 'category')),
  value text NOT NULL DEFAULT '',
  total_count integer NOT NULL DEFAULT 0,
  max_sort_at timestamptz,
  generated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, scope_key)
);

CREATE INDEX IF NOT EXISTS info_scopes_lookup_idx
  ON remote_poc.info_scopes (version_id, platform, dimension, value);

CREATE TABLE IF NOT EXISTS remote_poc.info_card_items (
  version_id uuid NOT NULL REFERENCES remote_poc.info_read_model_versions(version_id) ON DELETE CASCADE,
  item_id text NOT NULL,
  card_json jsonb NOT NULL,
  platform text NOT NULL,
  source text,
  sort_at timestamptz,
  fetched_at timestamptz,
  published_at timestamptz,
  relevance_score double precision,
  PRIMARY KEY (version_id, item_id)
);

CREATE INDEX IF NOT EXISTS info_card_items_platform_sort_idx
  ON remote_poc.info_card_items (
    version_id,
    platform,
    sort_at DESC NULLS LAST,
    fetched_at DESC NULLS LAST,
    relevance_score DESC NULLS LAST
  );

CREATE TABLE IF NOT EXISTS remote_poc.info_scope_items (
  version_id uuid NOT NULL REFERENCES remote_poc.info_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  rank integer NOT NULL,
  item_id text NOT NULL,
  sort_at timestamptz,
  fetched_at timestamptz,
  relevance_score double precision,
  PRIMARY KEY (version_id, scope_key, rank)
);

CREATE INDEX IF NOT EXISTS info_scope_items_item_idx
  ON remote_poc.info_scope_items (version_id, item_id);

CREATE INDEX IF NOT EXISTS info_scope_items_scope_rank_idx
  ON remote_poc.info_scope_items (version_id, scope_key, rank);

-- Rollback:
-- DROP TABLE IF EXISTS remote_poc.info_scope_items;
-- DROP TABLE IF EXISTS remote_poc.info_card_items;
-- DROP TABLE IF EXISTS remote_poc.info_scopes;
-- DROP TABLE IF EXISTS remote_poc.info_read_model_state;
-- DROP TABLE IF EXISTS remote_poc.info_read_model_versions;
