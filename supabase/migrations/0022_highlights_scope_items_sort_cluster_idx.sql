-- 2026-05-27: support append-only highlights scope membership reads.
--
-- Highlights read paths now order by stable event sort keys instead of dense
-- rank. Keep a matching index so reducing write amplification does not move
-- the cost to runtime sorting.

set search_path = remote_poc, extensions, public;
set statement_timeout = '10min';

CREATE INDEX IF NOT EXISTS highlights_scope_items_scope_sort_cluster_idx
  ON remote_poc.highlights_scope_items (
    version_id,
    scope_key,
    sort_at DESC NULLS LAST,
    cluster_id DESC
  )
  INCLUDE (rank);

-- Rollback:
-- DROP INDEX IF EXISTS remote_poc.highlights_scope_items_scope_sort_cluster_idx;
