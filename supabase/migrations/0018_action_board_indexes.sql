-- Action Tab board query indexes.
--
-- Why:
--   /api/actions/board needs the top N actions per direction ordered by:
--     status rank, priority rank, created_at desc.
--   The current production plan has to Seq Scan + Sort remote_poc.actions for
--   each board direction because existing indexes only cover status, priority,
--   created_at, and user_id independently.
--
-- Intended effect:
--   Make the LATERAL LIMIT/OFFSET item lookup in src/remote_db.py use an index
--   path for each direction instead of repeatedly scanning the 35MB actions
--   table body.
--
-- Idempotent. CONCURRENTLY avoids blocking normal reads/writes.
-- Rollback:
--   DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_actions_board_direction_sort_idx;
--   DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_actions_board_user_direction_sort_idx;

create index concurrently if not exists remote_poc_actions_board_direction_sort_idx
  on remote_poc.actions (
    (coalesce(direction, '_uncategorized')),
    (case status
      when 'dispatched' then 0
      when 'executing' then 0
      when 'confirmed' then 0
      when 'pending' then 1
      when 'done' then 2
      when 'failed' then 3
      when 'dismissed' then 4
      else 5
    end),
    (case priority
      when 'high' then 0
      when 'medium' then 1
      when 'low' then 2
      when 'bug' then 3
      else 4
    end),
    created_at desc
  );

create index concurrently if not exists remote_poc_actions_board_user_direction_sort_idx
  on remote_poc.actions (
    user_id,
    (coalesce(direction, '_uncategorized')),
    (case status
      when 'dispatched' then 0
      when 'executing' then 0
      when 'confirmed' then 0
      when 'pending' then 1
      when 'done' then 2
      when 'failed' then 3
      when 'dismissed' then 4
      else 5
    end),
    (case priority
      when 'high' then 0
      when 'medium' then 1
      when 'low' then 2
      when 'bug' then 3
      else 4
    end),
    created_at desc
  )
  where user_id is not null;
