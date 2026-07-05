-- BF-0515-2: composite index covering /api/feed sort order
-- Why: query_feed() in src/remote_db.py orders by
--   (fetched_at DESC NULLS LAST, published_at DESC NULLS LAST)
-- Existing indexes cover (visible, fetched_at, relevance_score) — wrong tiebreaker.
-- Without this index, Postgres scans all visible rows (~60k) and Sorts in memory
-- (~5s cold buffer hit, ~70ms warm). With this index, planner does Index Scan +
-- LIMIT 20 with no Sort node, target ~50ms cold and ~5ms warm.
--
-- Idempotent. CONCURRENTLY → no write blocking.
-- Rollback: DROP INDEX CONCURRENTLY remote_poc.remote_poc_items_visible_fetched_published_idx;

create index concurrently if not exists remote_poc_items_visible_fetched_published_idx
  on remote_poc.items (fetched_at desc nulls last, published_at desc nulls last)
  where visible = 1;
