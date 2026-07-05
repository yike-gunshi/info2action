-- 2026-05-27: support append-only info scope membership reads.
--
-- P3 for Supabase IO spikes changes info_scope_items.rank from the page order
-- authority into a uniqueness slot for delta appends. Readers order by the
-- stable item sort keys instead, so they need a matching composite index.

set search_path = remote_poc, extensions, public;
set statement_timeout = '10min';

CREATE INDEX IF NOT EXISTS info_scope_items_scope_sort_idx
  ON remote_poc.info_scope_items (
    version_id,
    scope_key,
    sort_at DESC NULLS LAST,
    fetched_at DESC NULLS LAST,
    relevance_score DESC NULLS LAST,
    item_id DESC
  )
  INCLUDE (rank);

-- Rollback:
-- DROP INDEX IF EXISTS remote_poc.info_scope_items_scope_sort_idx;
