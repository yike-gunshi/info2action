-- Remote-only admin/auth performance indexes. Additive and idempotent.

create schema if not exists remote_poc;

set search_path = remote_poc, public;
set statement_timeout = '5min';

create index if not exists remote_poc_fetch_runs_started_idx
  on remote_poc.fetch_runs(started_at desc, id desc);

create index if not exists remote_poc_invite_codes_created_idx
  on remote_poc.invite_codes(created_at desc);

create index if not exists remote_poc_embedding_usage_run_created_idx
  on remote_poc.embedding_usage_logs(run_id, created_at desc);

create index if not exists remote_poc_embedding_usage_created_id_idx
  on remote_poc.embedding_usage_logs(created_at desc, id desc);

