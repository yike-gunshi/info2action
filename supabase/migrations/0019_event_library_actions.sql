-- v18.1 event-library-actions
-- Cluster favorite state for personal history/favorites library.

alter table remote_poc.cluster_status
  add column if not exists starred_at timestamptz;

create index if not exists remote_poc_cluster_status_user_clicked_idx
  on remote_poc.cluster_status(user_id, clicked_at desc)
  where clicked_at is not null;

create index if not exists remote_poc_cluster_status_user_starred_idx
  on remote_poc.cluster_status(user_id, starred_at desc)
  where starred_at is not null;
