-- Remote-only target schema coverage.
-- 0001 created the POC read schema for feed/events. This migration fills the
-- remaining production persistence surface so future direct writers do not
-- depend on local SQLite tables.

create schema if not exists remote_poc;

-- Sequences for tables that were imported with explicit SQLite ids during POC.
create sequence if not exists remote_poc.fetch_runs_id_seq;
alter table remote_poc.fetch_runs
  alter column id set default nextval('remote_poc.fetch_runs_id_seq');
select setval(
  'remote_poc.fetch_runs_id_seq',
  greatest(coalesce((select max(id) from remote_poc.fetch_runs), 0), 1),
  true
);

create sequence if not exists remote_poc.clusters_id_seq;
alter table remote_poc.clusters
  alter column id set default nextval('remote_poc.clusters_id_seq');
select setval(
  'remote_poc.clusters_id_seq',
  greatest(coalesce((select max(id) from remote_poc.clusters), 0), 1),
  true
);

create sequence if not exists remote_poc.cluster_judge_log_id_seq;
alter table remote_poc.cluster_judge_log
  alter column id set default nextval('remote_poc.cluster_judge_log_id_seq');
select setval(
  'remote_poc.cluster_judge_log_id_seq',
  greatest(coalesce((select max(id) from remote_poc.cluster_judge_log), 0), 1),
  true
);

alter table remote_poc.items add column if not exists ai_error_count integer not null default 0;
alter table remote_poc.items add column if not exists ai_last_error text;
alter table remote_poc.items add column if not exists ai_last_error_at timestamptz;
alter table remote_poc.items add column if not exists ai_retry_after timestamptz;
alter table remote_poc.items add column if not exists ai_dimensions jsonb;
alter table remote_poc.items add column if not exists ai_relevance double precision;
alter table remote_poc.items add column if not exists asr_text text;
alter table remote_poc.items add column if not exists asr_status text;
alter table remote_poc.items add column if not exists asr_duration_sec integer;
alter table remote_poc.items add column if not exists asr_cost_yuan double precision;
alter table remote_poc.items add column if not exists asr_attempted_at timestamptz;
alter table remote_poc.items add column if not exists asr_failed_reason text;
alter table remote_poc.items add column if not exists asr_provider text default 'doubao-seedasr-bigmodel';
alter table remote_poc.items add column if not exists asr_segments jsonb;
alter table remote_poc.items add column if not exists asr_text_cn text;
alter table remote_poc.items add column if not exists asr_segments_cn jsonb;
alter table remote_poc.items add column if not exists cluster_locked integer not null default 0;
alter table remote_poc.items add column if not exists stage_a_state text;
alter table remote_poc.items add column if not exists stage_a_failed_at timestamptz;
alter table remote_poc.items alter column visible set default 1;

create index if not exists remote_poc_items_source_idx
  on remote_poc.items(source);
create index if not exists remote_poc_items_ai_category_idx
  on remote_poc.items(ai_category);
create index if not exists remote_poc_items_category_fetched_idx
  on remote_poc.items(ai_category, fetched_at desc);
create index if not exists remote_poc_items_visible_idx
  on remote_poc.items(visible);
create index if not exists remote_poc_items_asr_status_idx
  on remote_poc.items(asr_status)
  where asr_status is not null;
create index if not exists remote_poc_items_stage_a_state_idx
  on remote_poc.items(stage_a_state)
  where stage_a_state is not null;

create table if not exists remote_poc.fetch_run_items (
  run_id bigint not null references remote_poc.fetch_runs(id) on delete cascade,
  item_id text not null references remote_poc.items(id) on delete cascade,
  platform text,
  source text,
  was_inserted integer not null default 0,
  recorded_at timestamptz not null default now(),
  primary key (run_id, item_id)
);
create index if not exists remote_poc_fetch_run_items_run_source_idx
  on remote_poc.fetch_run_items(run_id, platform, source, was_inserted);

create table if not exists remote_poc.search_keywords (
  keyword text not null,
  platform text not null,
  last_used_at timestamptz,
  primary key (keyword, platform)
);

create table if not exists remote_poc.feedback (
  id bigserial primary key,
  item_id text not null references remote_poc.items(id) on delete cascade,
  type text not null,
  topic text,
  text text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_feedback_item_idx
  on remote_poc.feedback(item_id);
create index if not exists remote_poc_feedback_type_idx
  on remote_poc.feedback(type);

create table if not exists remote_poc.briefings (
  id text primary key,
  date text,
  insights text,
  suggestions text,
  input_count integer,
  model text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_briefings_date_idx
  on remote_poc.briefings(date);

create table if not exists remote_poc.actions (
  id text primary key,
  user_id text,
  source_type text not null,
  source_item_ids jsonb,
  source_id text,
  cluster_version integer,
  is_stale integer not null default 0,
  original_title text,
  original_prompt text,
  original_reason text,
  original_priority text,
  title text not null,
  action_type text not null,
  related_project text,
  prompt text not null,
  reason text,
  priority text default 'medium',
  direction text default '_uncategorized',
  direction_label text default '待归类',
  status text default 'pending',
  execution_tool text default 'codex',
  execution_result text,
  execution_exit_code integer,
  execution_model text,
  execution_duration_seconds integer,
  session_id text,
  project_context text,
  project_context_updated_at timestamptz,
  discord_thread_id text,
  discord_thread_url text,
  dispatched_at timestamptz,
  created_at timestamptz not null default now(),
  confirmed_at timestamptz,
  executed_at timestamptz,
  completed_at timestamptz,
  dismissed_at timestamptz
);
create index if not exists remote_poc_actions_user_idx
  on remote_poc.actions(user_id);
create index if not exists remote_poc_actions_status_idx
  on remote_poc.actions(status);
create index if not exists remote_poc_actions_created_at_idx
  on remote_poc.actions(created_at desc);
create index if not exists remote_poc_actions_priority_idx
  on remote_poc.actions(priority);

create table if not exists remote_poc.action_logs (
  id bigserial primary key,
  action_id text not null references remote_poc.actions(id) on delete cascade,
  event_type text not null,
  detail_json jsonb,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_action_logs_action_idx
  on remote_poc.action_logs(action_id);

create table if not exists remote_poc.action_feedback (
  id bigserial primary key,
  action_id text not null references remote_poc.actions(id) on delete cascade,
  phase text not null,
  rating text not null,
  comment text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_action_feedback_action_idx
  on remote_poc.action_feedback(action_id);

create table if not exists remote_poc.interests (
  id bigserial primary key,
  user_id text,
  name text not null,
  description text,
  keywords jsonb,
  sort text default 'relevance',
  item_limit integer default 30,
  scope text default 'all',
  enabled integer default 1,
  scan_status text default 'pending',
  last_scan_at timestamptz,
  suggestion text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_interests_user_idx
  on remote_poc.interests(user_id);

create table if not exists remote_poc.interest_matches (
  interest_id bigint not null references remote_poc.interests(id) on delete cascade,
  item_id text not null references remote_poc.items(id) on delete cascade,
  relevance_score double precision,
  is_new integer default 1,
  matched_at timestamptz not null default now(),
  primary key (interest_id, item_id)
);
create index if not exists remote_poc_interest_matches_interest_idx
  on remote_poc.interest_matches(interest_id);
create index if not exists remote_poc_interest_matches_score_idx
  on remote_poc.interest_matches(interest_id, relevance_score desc);

create table if not exists remote_poc.health_log (
  id bigserial primary key,
  timestamp timestamptz not null default now(),
  platform text not null,
  old_status text,
  new_status text not null,
  message text,
  source text
);
create index if not exists remote_poc_health_log_platform_idx
  on remote_poc.health_log(platform);
create index if not exists remote_poc_health_log_timestamp_idx
  on remote_poc.health_log(timestamp desc);

create table if not exists remote_poc.users (
  id text primary key,
  username text unique not null,
  email text unique,
  password_hash text not null,
  role text default 'user',
  discord_bot_token_enc text,
  email_verified integer default 0,
  verification_code text,
  verification_code_expires timestamptz,
  reset_token text,
  reset_token_expires timestamptz,
  created_at timestamptz not null default now(),
  last_login_at timestamptz
);

create table if not exists remote_poc.invite_codes (
  code text primary key,
  created_by text references remote_poc.users(id),
  used_by text references remote_poc.users(id),
  max_uses integer default 1,
  used_count integer default 0,
  expires_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists remote_poc.sessions (
  id text primary key,
  user_id text not null references remote_poc.users(id) on delete cascade,
  token_type text not null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_sessions_user_idx
  on remote_poc.sessions(user_id);
create index if not exists remote_poc_sessions_expires_idx
  on remote_poc.sessions(expires_at);

create table if not exists remote_poc.user_profiles (
  user_id text primary key references remote_poc.users(id) on delete cascade,
  role text,
  interests jsonb,
  tools jsonb,
  manifest jsonb,
  onboarding_completed integer default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists remote_poc.asr_usage (
  user_id text not null default '0',
  date_cst text not null,
  seconds_used integer not null default 0,
  updated_at timestamptz not null,
  primary key (user_id, date_cst)
);
create index if not exists remote_poc_asr_usage_date_idx
  on remote_poc.asr_usage(date_cst);

alter table remote_poc.clusters add column if not exists ai_summary_draft text;
alter table remote_poc.clusters add column if not exists ai_title_draft text;
alter table remote_poc.clusters add column if not exists ai_key_points_draft text;
alter table remote_poc.clusters add column if not exists last_summary_warnings_json jsonb;
alter table remote_poc.clusters add column if not exists pending_is_visible_in_feed integer;
alter table remote_poc.clusters add column if not exists pending_summary_warnings_json jsonb;

alter table remote_poc.cluster_items add column if not exists source_identity text;
alter table remote_poc.cluster_items add column if not exists join_decision_id text;

create table if not exists remote_poc.settings (
  key text primary key,
  value text,
  updated_at timestamptz not null default now()
);

create table if not exists remote_poc.clusters_v2 (
  id bigserial primary key,
  centroid extensions.vector(1536),
  dominant_category text,
  event_summary text,
  event_certainty text,
  member_count integer not null default 0,
  created_at timestamptz not null default now(),
  last_member_added_at timestamptz,
  stage_p_state text,
  stage_p_run_at timestamptz,
  stage_p_failed_reason text
);
create index if not exists remote_poc_clusters_v2_stage_p_state_idx
  on remote_poc.clusters_v2(stage_p_state)
  where stage_p_state is not null;

create table if not exists remote_poc.cluster_items_v2 (
  cluster_id bigint not null references remote_poc.clusters_v2(id) on delete cascade,
  item_id text not null references remote_poc.items(id) on delete cascade,
  added_at timestamptz not null default now(),
  joined_cosine double precision,
  removed_at timestamptz,
  removed_reason text,
  primary key (cluster_id, item_id)
);
create index if not exists remote_poc_cluster_items_v2_item_idx
  on remote_poc.cluster_items_v2(item_id);
create index if not exists remote_poc_cluster_items_v2_visible_idx
  on remote_poc.cluster_items_v2(cluster_id)
  where removed_at is null;

create table if not exists remote_poc.cluster_p_log (
  id bigserial primary key,
  cluster_id bigint not null,
  item_id text,
  action text not null,
  reason text,
  llm_model text,
  raw_response text,
  created_at timestamptz not null default now()
);
create index if not exists remote_poc_cluster_p_log_cluster_idx
  on remote_poc.cluster_p_log(cluster_id);
