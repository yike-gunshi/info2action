-- AI search v2 durable retrieval jobs and writing-time quotas.
-- Execute only with explicit user authorization, via the 5432 SESSION port:
-- use SUPABASE_DB_DIRECT_URL, or replace SUPABASE_DB_URL's 6543 port with 5432.
-- Do not use the 6543 transaction port.
--   psql "$SUPABASE_DB_DIRECT_URL" -v ON_ERROR_STOP=1 -f supabase/migrations/2026101100_ai_search_v2.sql
-- This file owns its transaction; all settings are transaction-local.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $$
DECLARE
    status_constraint record;
BEGIN
    -- conkey identifies the referenced column, regardless of CHECK names or
    -- whether the old CHECK mentions additional columns as well as status.
    FOR status_constraint IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid
        WHERE c.conrelid = 'remote_poc.ai_search_jobs'::regclass
          AND c.contype = 'c' AND a.attname = 'status'
          AND a.attnum = ANY(c.conkey)
    LOOP
        EXECUTE format('ALTER TABLE remote_poc.ai_search_jobs DROP CONSTRAINT %I',
                       status_constraint.conname);
    END LOOP;
END;
$$;

ALTER TABLE remote_poc.ai_search_jobs
    ADD CONSTRAINT ai_search_jobs_status_check
        CHECK (status IN ('retrieving', 'insufficient', 'queued', 'running', 'done', 'failed', 'unavailable')),
    ADD COLUMN write_queued_at timestamptz,
    ADD COLUMN retrieval jsonb,
    ALTER COLUMN query_lexical DROP NOT NULL;

UPDATE remote_poc.ai_search_jobs SET write_queued_at = created_at;
COMMIT;

-- Rollback requires explicit authorization and stopping v2 submissions/workers.
-- Archive v2 retrieval records first; dropping the columns loses those records.
-- BEGIN;
-- SET LOCAL lock_timeout = '5s';
-- SET LOCAL statement_timeout = '60s';
-- UPDATE remote_poc.ai_search_jobs SET status='failed', error=COALESCE(error, 'retrieval_failed'),
--     finished_at=COALESCE(finished_at, now()) WHERE status IN ('retrieving', 'insufficient');
-- UPDATE remote_poc.ai_search_jobs SET query_lexical=query WHERE query_lexical IS NULL;
-- ALTER TABLE remote_poc.ai_search_jobs DROP CONSTRAINT ai_search_jobs_status_check,
--     ADD CONSTRAINT ai_search_jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed', 'unavailable')),
--     ALTER COLUMN query_lexical SET NOT NULL, DROP COLUMN write_queued_at, DROP COLUMN retrieval;
-- COMMIT;
