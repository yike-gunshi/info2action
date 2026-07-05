-- Phase 0 Supabase remote database POC.
-- This migration intentionally uses a separate schema (`remote_poc`) so the
-- experiment can be dropped/re-run without implying a production cutover.

create schema if not exists extensions;
create extension if not exists vector with schema extensions;

create schema if not exists remote_poc;

create table if not exists remote_poc.items (
  id text primary key,
  user_id text,
  platform text not null,
  source text not null,
  fetch_run_id bigint,
  title text,
  content text,
  author_name text,
  author_id text,
  author_avatar text,
  url text,
  cover_url text,
  description text,
  media_json jsonb,
  metrics_json jsonb,
  tags_json jsonb,
  lang text,
  detail_json jsonb,
  comments_json jsonb,
  ai_summary text,
  ai_key_points text,
  ai_category text,
  ai_keywords text,
  ai_categories jsonb,
  ai_subcategories jsonb,
  multi_l1_reason text,
  ai_extracted jsonb,
  content_type text,
  ai_quality_score double precision,
  visible integer,
  relevance_score double precision,
  embedding extensions.vector(1536),
  embedding_provider text,
  embedding_model text,
  embedding_input_variant text,
  embedding_generated_at timestamptz,
  canonical_url text,
  cluster_id bigint,
  fetched_at timestamptz,
  published_at timestamptz,
  created_at timestamptz
);

alter table remote_poc.items add column if not exists description text;
alter table remote_poc.items add column if not exists ai_quality_score double precision;

create table if not exists remote_poc.clusters (
  id bigint primary key,
  ai_title text,
  ai_summary text,
  ai_key_points text,
  live_version integer,
  doc_count integer,
  unique_source_count integer,
  platforms_json jsonb,
  cover_url text,
  first_doc_at timestamptz,
  last_doc_at timestamptz,
  last_updated_at timestamptz,
  is_visible_in_feed boolean,
  merged_into bigint,
  archived boolean,
  prompt_version text,
  representative_vector extensions.vector(1536),
  event_embedding extensions.vector(1536),
  created_run_id bigint,
  last_touched_run_id bigint,
  published_run_id bigint,
  published_at timestamptz,
  created_at timestamptz
);

create table if not exists remote_poc.cluster_items (
  cluster_id bigint not null references remote_poc.clusters(id) on delete cascade,
  item_id text not null references remote_poc.items(id) on delete cascade,
  rank_in_cluster integer,
  added_at timestamptz,
  is_primary_source boolean,
  source_identity text,
  join_decision_id text,
  primary key (cluster_id, item_id)
);

create table if not exists remote_poc.item_status (
  user_id text not null,
  item_id text not null references remote_poc.items(id) on delete cascade,
  read_at timestamptz,
  clicked_at timestamptz,
  starred_at timestamptz,
  hidden_at timestamptz,
  primary key (user_id, item_id)
);

create table if not exists remote_poc.cluster_status (
  user_id text not null,
  cluster_id bigint not null references remote_poc.clusters(id) on delete cascade,
  clicked_at timestamptz,
  starred_at timestamptz,
  last_seen_version integer not null default 0,
  primary key (user_id, cluster_id)
);

create table if not exists remote_poc.fetch_runs (
  id bigint primary key,
  started_at timestamptz,
  finished_at timestamptz,
  status text,
  stats_json jsonb,
  error_msg text
);

create table if not exists remote_poc.cluster_judge_log (
  id bigint primary key,
  item_id text,
  candidate_cluster_ids jsonb,
  llm_input_tokens integer,
  llm_output_tokens integer,
  matches_json jsonb,
  selected_cluster_id bigint,
  selection_reason text,
  possible_merge_candidates jsonb,
  decision_model text,
  created_at timestamptz
);

create table if not exists remote_poc.sync_runs (
  id bigserial primary key,
  source_db_path text,
  sample_name text,
  items_attempted integer not null default 0,
  clusters_attempted integer not null default 0,
  cluster_items_attempted integer not null default 0,
  item_status_attempted integer not null default 0,
  cluster_status_attempted integer not null default 0,
  fetch_runs_attempted integer not null default 0,
  judge_logs_attempted integer not null default 0,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  stats_json jsonb
);

alter table remote_poc.sync_runs add column if not exists item_status_attempted integer not null default 0;
alter table remote_poc.sync_runs add column if not exists cluster_status_attempted integer not null default 0;

create index if not exists remote_poc_items_platform_idx
  on remote_poc.items(platform);
create index if not exists remote_poc_items_fetched_at_idx
  on remote_poc.items(fetched_at desc);
create index if not exists remote_poc_items_fetch_run_id_idx
  on remote_poc.items(fetch_run_id);
create index if not exists remote_poc_items_cluster_id_idx
  on remote_poc.items(cluster_id)
  where cluster_id is not null;

create index if not exists remote_poc_clusters_visible_published_idx
  on remote_poc.clusters(is_visible_in_feed, published_at desc);
create index if not exists remote_poc_clusters_last_updated_idx
  on remote_poc.clusters(last_updated_at desc);
create index if not exists remote_poc_cluster_items_item_idx
  on remote_poc.cluster_items(item_id);
create index if not exists remote_poc_item_status_item_idx
  on remote_poc.item_status(item_id);
create index if not exists remote_poc_item_status_starred_idx
  on remote_poc.item_status(user_id, starred_at)
  where starred_at is not null;
create index if not exists remote_poc_cluster_status_cluster_idx
  on remote_poc.cluster_status(cluster_id);
create index if not exists remote_poc_judge_log_item_idx
  on remote_poc.cluster_judge_log(item_id);

-- HNSW is intentionally not created in the first smoke stage. Add it after
-- correctness checks if S1/S2 latency needs it:
--
-- create index concurrently remote_poc_clusters_repvec_hnsw_idx
--   on remote_poc.clusters
--   using hnsw (representative_vector extensions.vector_cosine_ops);

create or replace function remote_poc.match_clusters(
  query_embedding extensions.vector(1536),
  match_count integer default 10
)
returns table (
  id bigint,
  cosine_similarity double precision
)
language sql
stable
as $$
  select c.id,
         1 - (c.representative_vector <=> query_embedding) as cosine_similarity
    from remote_poc.clusters c
   where c.representative_vector is not null
     and coalesce(c.archived, false) = false
     and c.merged_into is null
   order by c.representative_vector <=> query_embedding
   limit match_count;
$$;
