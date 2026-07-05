-- SUPA-PERF-0531-P1-1: pgvector HNSW index for cluster recall.
--
-- Why: recall_top_k_clusters_remote() orders live clusters by
--   representative_vector <=> query_embedding.
-- Supabase Query Performance showed this path doing Seq Scan + Sort on
-- remote_poc.clusters. Keep the index partial so it matches the production
-- recall predicate and avoids archived/merged/null-vector rows.
--
-- Operational notes:
-- - Run outside an explicit transaction; CREATE INDEX CONCURRENTLY cannot run
--   inside BEGIN/COMMIT.
-- - Execute in a low-traffic window and watch pg_stat_activity.
-- - Rollback: DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_clusters_representative_vector_hnsw_idx;

set search_path = remote_poc, extensions, public;
set lock_timeout = '5s';
set statement_timeout = '45min';

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_clusters_representative_vector_hnsw_idx
  ON remote_poc.clusters
  USING hnsw (representative_vector extensions.vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE representative_vector IS NOT NULL
    AND COALESCE(archived, false) = false
    AND merged_into IS NULL;
