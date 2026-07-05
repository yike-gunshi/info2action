-- 2026-05-23: extend 信息 read model scope coverage.
--
-- `section_subcategory` covers type L2 pills such as products + chatbot.
-- `group_source` covers lingowhale group + channel/source combinations.

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.info_scopes
  DROP CONSTRAINT IF EXISTS info_scopes_dimension_check;

ALTER TABLE remote_poc.info_scopes
  ADD CONSTRAINT info_scopes_dimension_check
  CHECK (dimension IN (
    'all',
    'source',
    'group',
    'group_source',
    'category',
    'section_category',
    'section_subcategory'
  ));

-- Rollback:
-- ALTER TABLE remote_poc.info_scopes
--   DROP CONSTRAINT IF EXISTS info_scopes_dimension_check;
-- ALTER TABLE remote_poc.info_scopes
--   ADD CONSTRAINT info_scopes_dimension_check
--   CHECK (dimension IN ('all', 'source', 'group', 'category', 'section_category'));
