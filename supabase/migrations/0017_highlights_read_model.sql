-- 2026-05-23: versioned read model for the 精选 tab event timeline.
--
-- This projection keeps the existing /api/feed/events product semantics while
-- moving hot reads for the default timeline and single L1 category pills away
-- from live clusters/cluster_items/items joins.

set search_path = remote_poc, extensions, public;

CREATE TABLE IF NOT EXISTS remote_poc.highlights_read_model_versions (
  version_id uuid PRIMARY KEY,
  status text NOT NULL DEFAULT 'building'
    CHECK (status IN ('building', 'complete', 'error')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  max_cluster_updated_at timestamptz,
  window_days integer NOT NULL DEFAULT 30,
  min_github_stars integer NOT NULL DEFAULT 50,
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE TABLE IF NOT EXISTS remote_poc.highlights_read_model_state (
  key text PRIMARY KEY,
  active_version_id uuid NOT NULL REFERENCES remote_poc.highlights_read_model_versions(version_id) ON DELETE RESTRICT,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS remote_poc.highlights_scopes (
  version_id uuid NOT NULL REFERENCES remote_poc.highlights_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  dimension text NOT NULL CHECK (dimension IN ('all', 'category')),
  value text NOT NULL DEFAULT '',
  total_count integer NOT NULL DEFAULT 0,
  max_sort_at timestamptz,
  generated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, scope_key)
);

CREATE INDEX IF NOT EXISTS highlights_scopes_lookup_idx
  ON remote_poc.highlights_scopes (version_id, dimension, value);

CREATE TABLE IF NOT EXISTS remote_poc.highlights_scope_items (
  version_id uuid NOT NULL REFERENCES remote_poc.highlights_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  rank integer NOT NULL,
  cluster_id integer NOT NULL,
  sort_at timestamptz,
  card_json jsonb NOT NULL,
  PRIMARY KEY (version_id, scope_key, rank)
);

CREATE INDEX IF NOT EXISTS highlights_scope_items_cluster_idx
  ON remote_poc.highlights_scope_items (version_id, cluster_id);

CREATE INDEX IF NOT EXISTS highlights_scope_items_scope_sort_idx
  ON remote_poc.highlights_scope_items (version_id, scope_key, sort_at DESC NULLS LAST, rank);

-- Rollback:
-- DROP TABLE IF EXISTS remote_poc.highlights_scope_items;
-- DROP TABLE IF EXISTS remote_poc.highlights_scopes;
-- DROP TABLE IF EXISTS remote_poc.highlights_read_model_state;
-- DROP TABLE IF EXISTS remote_poc.highlights_read_model_versions;
