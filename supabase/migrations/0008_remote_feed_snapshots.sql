-- Remote feed read-model snapshots. Additive and idempotent.
--
-- These snapshots keep production runtime remote-only while avoiding repeated
-- multi-query base-table aggregation for anonymous first-page reads.

create schema if not exists remote_poc;

set search_path = remote_poc, public;
set statement_timeout = '5min';

create table if not exists remote_poc.feed_snapshots (
  snapshot_key text primary key,
  payload_json jsonb not null,
  generated_at timestamptz not null default now(),
  expires_at timestamptz,
  source_run_id bigint,
  meta_json jsonb not null default '{}'::jsonb
);

create index if not exists remote_poc_feed_snapshots_expires_idx
  on remote_poc.feed_snapshots(expires_at);
