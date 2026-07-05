-- BF-0704-6: PGroonga indexes for live search (/api/search docs + events).
--
-- Why: search goes through `(concat expr) ILIKE '%kw%'` on remote_poc.items /
-- remote_poc.clusters. The existing pg_trgm GIN indexes (0006) are used by the
-- planner, but:
--   1. Patterns shorter than 3 chars (e.g. 2-char CJK keywords like 微信)
--      yield no extractable trigram -> full scan, measured 11.8s on prod.
--   2. Cold-cache bitmap heap rechecks over thousands of matches take 5-8s,
--      blowing the 1.5s/4.5s statement timeouts -> search permanently degraded
--      (context_search_events_unavailable).
-- PGroonga's text_full_text_search_ops_v2 accelerates LIKE/ILIKE natively,
-- including short CJK patterns, so the SQL stays unchanged.
--
-- Operational notes:
-- - Run outside an explicit transaction; CREATE INDEX CONCURRENTLY cannot run
--   inside BEGIN/COMMIT.
-- - Execute in a low-traffic window; each build takes minutes on the ~1-2GB
--   tables and adds on the order of a few hundred MB of index per table.
-- - The pg_trgm indexes from 0006 are kept; the planner picks whichever is
--   cheaper per query.
-- - Rollback:
--     DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_clusters_search_pgroonga_idx;
--     DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_items_search_pgroonga_idx;

CREATE EXTENSION IF NOT EXISTS pgroonga WITH SCHEMA extensions;

set search_path = remote_poc, extensions, public;
set lock_timeout = '5s';
set statement_timeout = '45min';

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_clusters_search_pgroonga_idx
  ON remote_poc.clusters
  USING pgroonga (
    ((COALESCE(ai_title, '') || ' ' || COALESCE(ai_summary, '')))
    extensions.pgroonga_text_full_text_search_ops_v2
  );

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_items_search_pgroonga_idx
  ON remote_poc.items
  USING pgroonga (
    ((COALESCE(title, '') || ' ' || COALESCE(author_name, '') || ' ' || COALESCE(ai_summary, '') || ' ' || COALESCE(ai_keywords, '')))
    extensions.pgroonga_text_full_text_search_ops_v2
  );

-- BF-0704-6 (rev2): title-first event search. The concat (title+summary)
-- predicate detoasts every matched row's ai_summary during bitmap recheck --
-- >15s cold for high-frequency keywords (openai: 2292 matches). ai_title is
-- an inline column, so a title-only search rechecks without TOAST I/O
-- (measured 80ms warm / <1s cold with this index). context_search now
-- queries title-first and supplements from the concat index only when title
-- matches fall short of the page size.
CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_clusters_ai_title_search_pgroonga_idx
  ON remote_poc.clusters
  USING pgroonga (ai_title extensions.pgroonga_text_full_text_search_ops_v2);
