-- 2026-05-23: dedicated search text for 信息 tab read model search.
--
-- The previous info_search_v1 path searched by concatenating fields from
-- card_json at read time. That made common searches carry large JSONB values
-- through counting and ranking. This generated column keeps the searchable
-- text close to the read model and lets pg_trgm accelerate ILIKE queries.

create schema if not exists remote_poc;
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;

set search_path = remote_poc, extensions, public;
set statement_timeout = '10min';

ALTER TABLE remote_poc.info_card_items
  ADD COLUMN IF NOT EXISTS search_text text GENERATED ALWAYS AS (
    coalesce(card_json ->> 'title', '') || ' ' ||
    coalesce(card_json ->> 'author_name', '') || ' ' ||
    coalesce(card_json ->> 'description', '') || ' ' ||
    coalesce(card_json ->> 'ai_summary', '') || ' ' ||
    coalesce(card_json ->> 'ai_keywords', '')
  ) STORED;

CREATE INDEX IF NOT EXISTS info_card_items_search_trgm_idx
  ON remote_poc.info_card_items USING gin (search_text gin_trgm_ops);

-- Rollback:
-- DROP INDEX IF EXISTS remote_poc.info_card_items_search_trgm_idx;
-- ALTER TABLE remote_poc.info_card_items DROP COLUMN IF EXISTS search_text;
