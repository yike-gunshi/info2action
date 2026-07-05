-- Remote binary/object metadata and the formerly local feedback analysis store.
-- Binary bytes live in Supabase Storage; this schema records ownership/kind so
-- delete/migration jobs can reason about assets from Postgres.

create schema if not exists remote_poc;

create table if not exists remote_poc.remote_assets (
  object_path text primary key,
  bucket text not null default 'info2action-assets',
  content_type text,
  size_bytes bigint,
  source_item_id text references remote_poc.items(id) on delete set null,
  kind text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists remote_poc_remote_assets_item_idx
  on remote_poc.remote_assets(source_item_id);
create index if not exists remote_poc_remote_assets_kind_idx
  on remote_poc.remote_assets(kind);

create table if not exists remote_poc.item_feedback (
  id bigserial primary key,
  item_id text not null references remote_poc.items(id) on delete cascade,
  platform text,
  item_title text,
  item_author text,
  item_url text,
  action text not null,
  reason text,
  topic_at_time text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_item_feedback_item_idx
  on remote_poc.item_feedback(item_id);
create index if not exists remote_poc_item_feedback_action_idx
  on remote_poc.item_feedback(action);
create index if not exists remote_poc_item_feedback_created_idx
  on remote_poc.item_feedback(created_at desc);

create table if not exists remote_poc.system_feedback (
  id bigserial primary key,
  category text,
  description text,
  context_json jsonb,
  created_at timestamptz not null default now()
);

create table if not exists remote_poc.preference_signals (
  id bigserial primary key,
  signal_type text,
  target text,
  note text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_preference_signals_type_idx
  on remote_poc.preference_signals(signal_type);
create index if not exists remote_poc_preference_signals_target_idx
  on remote_poc.preference_signals(target);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'info2action-assets',
  'info2action-assets',
  false,
  null,
  array['image/jpeg', 'image/png', 'image/webp', 'audio/mpeg', 'audio/mp3', 'video/mp4']::text[]
)
on conflict (id) do update set
  public = excluded.public,
  allowed_mime_types = excluded.allowed_mime_types;
