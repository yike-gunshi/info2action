-- v30.0: cross-device highlights reading checkpoint.
set search_path = remote_poc, extensions, public;

create table if not exists remote_poc.reading_progress (
  user_id text not null,
  surface text not null,
  cluster_id bigint not null references remote_poc.clusters(id) on delete cascade,
  anchor_sort_at timestamptz not null,
  updated_at timestamptz not null default now(),
  primary key (user_id, surface)
);

create index if not exists idx_reading_progress_anchor
  on remote_poc.reading_progress (user_id, surface, anchor_sort_at desc);

-- Rollback:
-- drop table if exists remote_poc.reading_progress;
