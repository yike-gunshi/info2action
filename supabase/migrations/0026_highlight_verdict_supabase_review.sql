-- 2026-06-16: item-level Highlights verdicts and Supabase review loop.

set search_path = remote_poc, extensions, public;
set statement_timeout = '10min';

alter table remote_poc.items add column if not exists highlight_verdict text
  check (highlight_verdict is null or highlight_verdict in ('featured', 'borderline', 'drop'));
alter table remote_poc.items add column if not exists highlight_value_path text
  check (highlight_value_path is null or highlight_value_path in ('substantive', 'major_event', 'lead_value', 'none'));
alter table remote_poc.items add column if not exists highlight_uncertainty text
  check (highlight_uncertainty is null or highlight_uncertainty in ('none', 'thin_detail', 'needs_source', 'unverified_major_claim'));
alter table remote_poc.items add column if not exists highlight_include_in_highlights boolean not null default false;
alter table remote_poc.items add column if not exists highlight_reason text;
alter table remote_poc.items add column if not exists highlight_scores jsonb not null default '{}'::jsonb;
alter table remote_poc.items add column if not exists highlight_ai_relevant text
  check (highlight_ai_relevant is null or highlight_ai_relevant in ('yes', 'no'));
alter table remote_poc.items add column if not exists highlight_spam integer
  check (highlight_spam is null or highlight_spam between 1 and 3);
alter table remote_poc.items add column if not exists highlight_confidence double precision
  check (highlight_confidence is null or (highlight_confidence >= 0 and highlight_confidence <= 1));
alter table remote_poc.items add column if not exists highlight_prompt_version text;
alter table remote_poc.items add column if not exists highlight_model text;
alter table remote_poc.items add column if not exists highlight_scored_at timestamptz;
alter table remote_poc.items add column if not exists highlight_error_count integer not null default 0;
alter table remote_poc.items add column if not exists highlight_last_error text;
alter table remote_poc.items add column if not exists highlight_retry_after timestamptz;

create index if not exists remote_poc_items_highlight_include_idx
  on remote_poc.items (highlight_scored_at desc, id)
  where highlight_include_in_highlights is true;

create index if not exists remote_poc_items_highlight_retry_idx
  on remote_poc.items (highlight_retry_after, fetched_at desc)
  where highlight_verdict is null;

create table if not exists remote_poc.highlight_cluster_decisions (
  cluster_id integer primary key references remote_poc.clusters(id) on delete cascade,
  decision text not null check (decision in ('included', 'excluded', 'pending')),
  cluster_verdict text not null check (cluster_verdict in ('featured', 'positive_borderline', 'risk_borderline', 'drop', 'pending')),
  deciding_item_id text references remote_poc.items(id) on delete set null,
  reason text,
  verdict_counts_json jsonb not null default '{}'::jsonb,
  prompt_version text,
  model text,
  decided_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  snapshot_json jsonb not null default '{}'::jsonb
);

create index if not exists remote_poc_highlight_cluster_decisions_decision_idx
  on remote_poc.highlight_cluster_decisions (decision, decided_at desc);
create index if not exists remote_poc_highlight_cluster_decisions_verdict_idx
  on remote_poc.highlight_cluster_decisions (cluster_verdict, decided_at desc);

create table if not exists remote_poc.highlight_exclusion_reviews (
  id bigserial primary key,
  cluster_id integer not null references remote_poc.clusters(id) on delete cascade,
  machine_decision_at timestamptz,
  human_verdict text not null check (human_verdict in ('should_feature', 'confirmed_drop', 'unsure')),
  error_kind text,
  notes text,
  reviewer text,
  reviewed_at timestamptz not null default now()
);

create index if not exists remote_poc_highlight_exclusion_reviews_cluster_idx
  on remote_poc.highlight_exclusion_reviews (cluster_id, reviewed_at desc);
create index if not exists remote_poc_highlight_exclusion_reviews_verdict_idx
  on remote_poc.highlight_exclusion_reviews (human_verdict, reviewed_at desc);

alter table remote_poc.highlight_cluster_decisions enable row level security;
alter table remote_poc.highlight_exclusion_reviews enable row level security;

-- Rollback:
-- DROP TABLE IF EXISTS remote_poc.highlight_exclusion_reviews;
-- DROP TABLE IF EXISTS remote_poc.highlight_cluster_decisions;
-- DROP INDEX IF EXISTS remote_poc.remote_poc_items_highlight_retry_idx;
-- DROP INDEX IF EXISTS remote_poc.remote_poc_items_highlight_include_idx;
