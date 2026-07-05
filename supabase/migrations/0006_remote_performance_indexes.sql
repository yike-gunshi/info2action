-- Remote-only performance indexes for feed sections, search, event timeline,
-- cluster bundles, and item detail lookups. All changes are additive and
-- idempotent.

create schema if not exists remote_poc;
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;

set search_path = remote_poc, extensions, public;
set statement_timeout = '10min';

create index if not exists remote_poc_items_visible_fetch_rank_idx
  on remote_poc.items(visible, fetched_at desc, relevance_score desc)
  where ai_categories is not null;

create index if not exists remote_poc_items_category_fetch_rank_idx
  on remote_poc.items(ai_category, fetched_at desc, relevance_score desc)
  where visible = 1 and ai_categories is not null;

create index if not exists remote_poc_items_platform_fetch_rank_idx
  on remote_poc.items(platform, fetched_at desc, relevance_score desc)
  where visible = 1;

create index if not exists remote_poc_items_platform_source_fetch_idx
  on remote_poc.items(platform, source, fetched_at desc)
  where visible = 1;

create index if not exists remote_poc_items_ai_categories_gin_idx
  on remote_poc.items using gin (ai_categories jsonb_path_ops);

create index if not exists remote_poc_items_ai_subcategories_gin_idx
  on remote_poc.items using gin (ai_subcategories jsonb_path_ops);

create index if not exists remote_poc_items_search_trgm_idx
  on remote_poc.items using gin (
    (
      coalesce(title, '') || ' ' ||
      coalesce(author_name, '') || ' ' ||
      coalesce(ai_summary, '') || ' ' ||
      coalesce(ai_keywords::text, '')
    ) gin_trgm_ops
  );

create index if not exists remote_poc_clusters_feed_timeline_idx
  on remote_poc.clusters(
    is_visible_in_feed,
    published_at,
    archived,
    merged_into,
    last_updated_at desc,
    id desc
  );

create index if not exists remote_poc_clusters_timeline_expr_idx
  on remote_poc.clusters(
    (coalesce(last_doc_at, first_doc_at, last_updated_at)) desc,
    last_updated_at desc,
    id desc
  )
  where is_visible_in_feed = true
    and published_at is not null
    and coalesce(archived, false) = false
    and merged_into is null;

create index if not exists remote_poc_clusters_search_trgm_idx
  on remote_poc.clusters using gin (
    (coalesce(ai_title, '') || ' ' || coalesce(ai_summary, '')) gin_trgm_ops
  );

create index if not exists remote_poc_cluster_items_cluster_rank_idx
  on remote_poc.cluster_items(cluster_id, is_primary_source desc, rank_in_cluster, item_id);

create index if not exists remote_poc_cluster_items_item_cluster_idx
  on remote_poc.cluster_items(item_id, cluster_id);

create index if not exists remote_poc_cluster_status_user_cluster_idx
  on remote_poc.cluster_status(user_id, cluster_id);

create index if not exists remote_poc_cluster_status_user_clicked_idx
  on remote_poc.cluster_status(user_id, clicked_at desc)
  where clicked_at is not null;

create index if not exists remote_poc_cluster_status_user_starred_idx
  on remote_poc.cluster_status(user_id, starred_at desc)
  where starred_at is not null;

create index if not exists remote_poc_item_status_user_item_idx
  on remote_poc.item_status(user_id, item_id);
