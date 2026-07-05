-- Action detail read models: display-ready payloads for fast complete modal opens.
create table if not exists remote_poc.action_detail_read_models (
  action_id text not null references remote_poc.actions(id) on delete cascade,
  viewer_scope text not null default 'owner',
  owner_user_id text,
  payload jsonb not null,
  source_item_ids jsonb not null default '[]'::jsonb,
  payload_version integer not null default 1,
  built_at timestamptz not null default now(),
  source_updated_at timestamptz,
  primary key (action_id, viewer_scope)
);

create index if not exists remote_poc_action_detail_read_models_owner_idx
  on remote_poc.action_detail_read_models(owner_user_id);

create index if not exists remote_poc_action_detail_read_models_built_at_idx
  on remote_poc.action_detail_read_models(built_at desc);
