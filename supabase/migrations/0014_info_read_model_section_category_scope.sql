-- 2026-05-23: allow the 信息 tab type view to use a dedicated section scope.
--
-- `category` remains the multi-label platform pill scope. `section_category`
-- is the single-home top-level 信息/类型 scope, ranked globally by fetched_at.

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.info_scopes
  DROP CONSTRAINT IF EXISTS info_scopes_dimension_check;

ALTER TABLE remote_poc.info_scopes
  ADD CONSTRAINT info_scopes_dimension_check
  CHECK (dimension IN ('all', 'source', 'group', 'category', 'section_category'));

-- Rollback:
-- ALTER TABLE remote_poc.info_scopes
--   DROP CONSTRAINT IF EXISTS info_scopes_dimension_check;
-- ALTER TABLE remote_poc.info_scopes
--   ADD CONSTRAINT info_scopes_dimension_check
--   CHECK (dimension IN ('all', 'source', 'group', 'category'));
