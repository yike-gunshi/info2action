-- Apply each statement outside a transaction (CONCURRENTLY). No cron changes.
-- Source first paint and keyset pages use the same publication-time order.
CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_items_platform_live_order_idx
ON remote_poc.items (
  platform, (COALESCE(published_at, fetched_at)) DESC NULLS LAST,
  fetched_at DESC NULLS LAST, relevance_score DESC NULLS LAST, id DESC
) WHERE visible = 1;

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_items_platform_source_live_order_idx
ON remote_poc.items (
  platform, source, (COALESCE(published_at, fetched_at)) DESC NULLS LAST,
  fetched_at DESC NULLS LAST, relevance_score DESC NULLS LAST, id DESC
) WHERE visible = 1;

-- Rollback (also outside a transaction):
-- DROP INDEX CONCURRENTLY remote_poc.remote_poc_items_platform_live_order_idx;
-- DROP INDEX CONCURRENTLY remote_poc.remote_poc_items_platform_source_live_order_idx;
