-- 2026-05-25: versioned read model for the 行动 tab three-lane board.
--
-- The Action detail read model accelerates modal payloads. This projection is
-- separate: it prebuilds the list/board surface by viewer scope, date filter,
-- priority filter, status lane, and created_at rank.

set search_path = remote_poc, extensions, public;

CREATE TABLE IF NOT EXISTS remote_poc.action_board_read_model_versions (
  version_id uuid PRIMARY KEY,
  viewer_scope text NOT NULL DEFAULT 'owner'
    CHECK (viewer_scope IN ('owner', 'admin')),
  owner_user_id text,
  payload_version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'building'
    CHECK (status IN ('building', 'complete', 'error')),
  generated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  max_action_updated_at timestamptz,
  total_actions integer NOT NULL DEFAULT 0,
  meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  error_message text
);

CREATE INDEX IF NOT EXISTS action_board_versions_scope_generated_idx
  ON remote_poc.action_board_read_model_versions
  (viewer_scope, owner_user_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS remote_poc.action_board_read_model_state (
  key text PRIMARY KEY,
  active_version_id uuid NOT NULL
    REFERENCES remote_poc.action_board_read_model_versions(version_id) ON DELETE RESTRICT,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS remote_poc.action_board_scopes (
  version_id uuid NOT NULL
    REFERENCES remote_poc.action_board_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  date_filter text NOT NULL DEFAULT 'all',
  priority_filter text NOT NULL DEFAULT 'all',
  total_count integer NOT NULL DEFAULT 0,
  status_counts_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  generated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, scope_key)
);

CREATE INDEX IF NOT EXISTS action_board_scopes_lookup_idx
  ON remote_poc.action_board_scopes (version_id, date_filter, priority_filter);

CREATE TABLE IF NOT EXISTS remote_poc.action_board_scope_lanes (
  version_id uuid NOT NULL
    REFERENCES remote_poc.action_board_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  lane_slug text NOT NULL,
  lane_label text NOT NULL,
  total_count integer NOT NULL DEFAULT 0,
  generated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, scope_key, lane_slug)
);

CREATE TABLE IF NOT EXISTS remote_poc.action_board_scope_items (
  version_id uuid NOT NULL
    REFERENCES remote_poc.action_board_read_model_versions(version_id) ON DELETE CASCADE,
  scope_key text NOT NULL,
  lane_slug text NOT NULL,
  rank integer NOT NULL,
  action_id text NOT NULL REFERENCES remote_poc.actions(id) ON DELETE CASCADE,
  created_at timestamptz,
  card_json jsonb NOT NULL,
  PRIMARY KEY (version_id, scope_key, lane_slug, rank)
);

CREATE INDEX IF NOT EXISTS action_board_scope_items_action_idx
  ON remote_poc.action_board_scope_items (version_id, action_id);

CREATE INDEX IF NOT EXISTS action_board_scope_items_lookup_idx
  ON remote_poc.action_board_scope_items
  (version_id, scope_key, lane_slug, rank);

-- Rollback:
-- DROP TABLE IF EXISTS remote_poc.action_board_scope_items;
-- DROP TABLE IF EXISTS remote_poc.action_board_scope_lanes;
-- DROP TABLE IF EXISTS remote_poc.action_board_scopes;
-- DROP TABLE IF EXISTS remote_poc.action_board_read_model_state;
-- DROP TABLE IF EXISTS remote_poc.action_board_read_model_versions;
