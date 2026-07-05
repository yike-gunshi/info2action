-- Complete SQLite-to-remote sync support for local-only app data and mixed
-- embedding dimensions. The main feed/search path keeps the original
-- vector(1536) columns; these side tables preserve every raw float32 vector so
-- a later provider-specific search path can rebuild indexes without going back
-- to local SQLite.

create schema if not exists remote_poc;
create schema if not exists extensions;
create extension if not exists vector with schema extensions;

create table if not exists remote_poc.embedding_usage_logs (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  provider text not null,
  model text,
  mode text,
  source text,
  stage text,
  run_id bigint,
  caller_file text,
  caller_func text,
  input_count integer not null default 0,
  input_chars integer not null default 0,
  input_bytes integer not null default 0,
  estimated_tokens integer not null default 0,
  token_estimator text,
  output_count integer not null default 0,
  output_dim integer,
  status text not null,
  error text,
  latency_ms integer,
  price_yuan_per_1k_tokens double precision,
  estimated_cost_yuan double precision,
  item_ids_json jsonb
);
create index if not exists remote_poc_embedding_usage_created_idx
  on remote_poc.embedding_usage_logs(created_at desc);
create index if not exists remote_poc_embedding_usage_run_idx
  on remote_poc.embedding_usage_logs(run_id, created_at desc);
create index if not exists remote_poc_embedding_usage_source_idx
  on remote_poc.embedding_usage_logs(source, created_at desc);

create table if not exists remote_poc.item_embedding_store (
  item_id text primary key references remote_poc.items(id) on delete cascade,
  provider text,
  model text,
  input_variant text,
  dim integer not null,
  embedding_float4 bytea not null,
  embedding_1024 extensions.vector(1024),
  embedding_1536 extensions.vector(1536),
  embedding_2048 extensions.vector(2048),
  generated_at timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists remote_poc_item_embedding_store_dim_idx
  on remote_poc.item_embedding_store(dim);
create index if not exists remote_poc_item_embedding_store_provider_idx
  on remote_poc.item_embedding_store(provider);

create table if not exists remote_poc.cluster_vector_store (
  cluster_id bigint not null references remote_poc.clusters(id) on delete cascade,
  vector_kind text not null default 'representative',
  dim integer not null,
  vector_float4 bytea not null,
  vector_1024 extensions.vector(1024),
  vector_1536 extensions.vector(1536),
  vector_2048 extensions.vector(2048),
  updated_at timestamptz not null default now(),
  primary key (cluster_id, vector_kind)
);
create index if not exists remote_poc_cluster_vector_store_dim_idx
  on remote_poc.cluster_vector_store(dim);

create table if not exists remote_poc.cluster_v2_vector_store (
  cluster_id bigint primary key references remote_poc.clusters_v2(id) on delete cascade,
  dim integer not null,
  vector_float4 bytea not null,
  vector_1024 extensions.vector(1024),
  vector_1536 extensions.vector(1536),
  vector_2048 extensions.vector(2048),
  updated_at timestamptz not null default now()
);
create index if not exists remote_poc_cluster_v2_vector_store_dim_idx
  on remote_poc.cluster_v2_vector_store(dim);
