#!/usr/bin/env python3
"""Info Radar — SQLite database module."""
import json, os, sqlite3
from datetime import datetime, timedelta, timezone

import action_detail_read_model
from category_taxonomy import canonicalize_category, expand_query_categories
from time_utils import sort_key

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, 'data', 'feed.db')

# BF-0419-7: 服务器本地时区相对 UTC 的偏移小时数
# 当前部署均为北京时间服务器(+08:00) → -8 表示 "把'实际是本地时间但被当 UTC'的值减回真正 UTC"
# 约定: 所有 ingester 写入的 published_at/fetched_at 若不带时区后缀,默认是**服务器本地时间**
# ⚠️ 若未来服务器迁移 UTC 时区 → 改为 0;若 ingester 改为统一写 UTC ISO 带时区 → 删除 hack,
# 并走 BF-0419-9 的 ingest 层根治方案(见 docs/bugfix/backlog.md)
_LOCAL_TZ_OFFSET_HOURS = -8

# Cache for PRAGMA table_info checks (populated once per process)
_item_status_has_user_id: bool | None = None


def _normalize_item_category(item):
    """Return a copy with ai_category mapped to the active taxonomy.

    v4.0: also parse ai_categories/ai_subcategories/ai_extracted JSON fields
    if present, exposing them as arrays/dicts for frontend consumption.
    Old items (pre-v4) have these as NULL; we leave them as None so frontend
    can fallback to the legacy single ai_category field.
    """
    out = dict(item)
    # Legacy single-category canonicalization (kept for backwards compat)
    category = canonicalize_category(out.get('ai_category'))
    if category != out.get('ai_category'):
        out['ai_category'] = category
    # v4.0 multi-tag fields: parse JSON columns into arrays/dicts
    for col in ('ai_categories', 'ai_subcategories'):
        raw = out.get(col)
        if isinstance(raw, str) and raw:
            try:
                out[col] = json.loads(raw)
            except (ValueError, TypeError):
                out[col] = None
    raw_ex = out.get('ai_extracted')
    if isinstance(raw_ex, str) and raw_ex:
        try:
            out['ai_extracted'] = json.loads(raw_ex)
        except (ValueError, TypeError):
            out['ai_extracted'] = None
    return out


# v15.0 BF-0424-EMB-BLOB: items table now has BLOB columns (embedding) that are
# never consumed by the frontend. JSONResponse / fastapi default encoder cannot
# serialize bytes → 500. Strip server-only BLOB columns whenever an item dict
# is about to leave the backend as JSON.
#
# Why a dedicated helper instead of inline `item.pop('embedding')`:
#   1. Single source of truth — future BLOB columns (e.g. thumbnail_blob) only
#      need to update this set, not every endpoint.
#   2. Mirror columns ('embedding_provider' is TEXT and JSON-safe but is part
#      of the same server-only concept; we keep it for backend ranking, but
#      future server-only columns can be added here.
#   3. Defensive: callers that pass partial dicts (no embedding key) are no-ops.
_SERVER_ONLY_BLOB_COLUMNS = ('embedding',)


def strip_blob_columns(item):
    """Return *item* with server-only BLOB columns removed (in-place).

    The caller is expected to have just done ``dict(row)`` from a
    ``SELECT i.* FROM items …`` query. Frontend never reads the embedding
    bytes; keeping them in the response is both wasted bandwidth AND a JSON
    serialization bomb (TypeError: Object of type bytes is not JSON serializable).

    Idempotent and safe on dicts where ``embedding`` is absent or NULL.
    """
    if not isinstance(item, dict):
        return item
    for col in _SERVER_ONLY_BLOB_COLUMNS:
        item.pop(col, None)
    return item


def _check_item_status_has_user_id(conn) -> bool:
    """Check if item_status table has user_id column (cached)."""
    global _item_status_has_user_id
    if _item_status_has_user_id is None:
        _item_status_has_user_id = any(
            row[1] == 'user_id'
            for row in conn.execute("PRAGMA table_info(item_status)").fetchall()
        )
    return _item_status_has_user_id


def _item_status_join(conn, user_id=None, *, item_alias='i', status_alias='s'):
    """Return a user-safe item_status LEFT JOIN and params.

    When item_status is user-scoped, anonymous reads must not join every user's
    status row; that duplicates feed items and inflates display counts.
    """
    has_user_id = _check_item_status_has_user_id(conn)
    if has_user_id and user_id:
        return (
            f"LEFT JOIN item_status {status_alias} "
            f"ON {item_alias}.id = {status_alias}.item_id AND {status_alias}.user_id = ?",
            [user_id],
        )
    if has_user_id:
        return f"LEFT JOIN item_status {status_alias} ON 1=0", []
    return (
        f"LEFT JOIN item_status {status_alias} "
        f"ON {item_alias}.id = {status_alias}.item_id",
        [],
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  platform TEXT NOT NULL,
  source TEXT NOT NULL,
  fetch_run_id INTEGER,
  title TEXT,
  content TEXT,
  author_name TEXT,
  author_id TEXT,
  author_avatar TEXT,
  url TEXT,
  cover_url TEXT,
  media_json TEXT,
  metrics_json TEXT,
  tags_json TEXT,
  lang TEXT,
  detail_json TEXT,
  comments_json TEXT,
  ai_summary TEXT,
  ai_error_count INTEGER DEFAULT 0,
  ai_last_error TEXT,
  ai_last_error_at TEXT,
  ai_retry_after TEXT,
  ai_dimensions TEXT,
  relevance_score REAL,
  fetched_at TEXT NOT NULL,
  published_at TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_platform ON items(platform);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON items(fetched_at);

CREATE TABLE IF NOT EXISTS item_status (
  item_id TEXT PRIMARY KEY REFERENCES items(id),
  read_at TEXT,
  clicked_at TEXT,
  starred_at TEXT,
  hidden_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT DEFAULT 'running',
  stats_json TEXT,
  error_msg TEXT
);

CREATE TABLE IF NOT EXISTS fetch_run_items (
  run_id      INTEGER NOT NULL REFERENCES fetch_runs(id) ON DELETE CASCADE,
  item_id     TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  platform    TEXT,
  source      TEXT,
  was_inserted INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (run_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_fetch_run_items_run_source
  ON fetch_run_items(run_id, platform, source, was_inserted);

CREATE TABLE IF NOT EXISTS embedding_usage_logs (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at           TEXT NOT NULL DEFAULT (datetime('now')),
  provider             TEXT NOT NULL,
  model                TEXT,
  mode                 TEXT,
  source               TEXT,
  stage                TEXT,
  run_id               INTEGER,
  caller_file          TEXT,
  caller_func          TEXT,
  input_count          INTEGER NOT NULL DEFAULT 0,
  input_chars          INTEGER NOT NULL DEFAULT 0,
  input_bytes          INTEGER NOT NULL DEFAULT 0,
  estimated_tokens     INTEGER NOT NULL DEFAULT 0,
  token_estimator      TEXT,
  output_count         INTEGER NOT NULL DEFAULT 0,
  output_dim           INTEGER,
  status               TEXT NOT NULL,
  error                TEXT,
  latency_ms           INTEGER,
  price_yuan_per_1k_tokens REAL,
  estimated_cost_yuan  REAL,
  item_ids_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_created
  ON embedding_usage_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_run
  ON embedding_usage_logs(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_embedding_usage_source
  ON embedding_usage_logs(source, created_at DESC);

CREATE TABLE IF NOT EXISTS search_keywords (
  keyword TEXT NOT NULL,
  platform TEXT NOT NULL,
  last_used_at TEXT,
  PRIMARY KEY (keyword, platform)
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL REFERENCES items(id),
  type TEXT NOT NULL,  -- 'positive', 'irrelevant', 'low_quality', 'text', 'should_feature'
  topic TEXT,
  text TEXT,  -- free-text feedback
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_item ON feedback(item_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(type);
CREATE INDEX IF NOT EXISTS idx_status_starred_at ON item_status(starred_at);

CREATE TABLE IF NOT EXISTS briefings (
  id          TEXT PRIMARY KEY,
  date        TEXT,
  insights    TEXT,
  suggestions TEXT,
  input_count INTEGER,
  model       TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  source_type TEXT NOT NULL,
  source_item_ids TEXT,

  original_title TEXT,
  original_prompt TEXT,
  original_reason TEXT,
  original_priority TEXT,

  title TEXT NOT NULL,
  action_type TEXT NOT NULL,
  related_project TEXT,
  prompt TEXT NOT NULL,
  steps TEXT,
  reason TEXT,
  priority TEXT DEFAULT 'medium',

  direction TEXT DEFAULT '_uncategorized',
  direction_label TEXT DEFAULT '待归类',

  status TEXT DEFAULT 'pending',
  execution_tool TEXT DEFAULT 'codex',
  execution_result TEXT,
  execution_exit_code INTEGER,
  execution_model TEXT,
  execution_duration_seconds INTEGER,
  session_id TEXT,
  project_context TEXT,
  project_context_updated_at TEXT,

  created_at TEXT DEFAULT (datetime('now')),
  confirmed_at TEXT,
  executed_at TEXT,
  completed_at TEXT,
  dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
CREATE INDEX IF NOT EXISTS idx_actions_created_at ON actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_priority ON actions(priority);

CREATE TABLE IF NOT EXISTS action_detail_read_models (
  action_id TEXT NOT NULL,
  viewer_scope TEXT NOT NULL DEFAULT 'owner',
  owner_user_id TEXT,
  payload_json TEXT NOT NULL,
  source_item_ids TEXT NOT NULL DEFAULT '[]',
  payload_version INTEGER NOT NULL DEFAULT 1,
  built_at TEXT DEFAULT (datetime('now')),
  source_updated_at TEXT,
  PRIMARY KEY (action_id, viewer_scope),
  FOREIGN KEY (action_id) REFERENCES actions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_action_detail_read_models_owner
  ON action_detail_read_models(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_action_detail_read_models_built_at
  ON action_detail_read_models(built_at DESC);

CREATE TABLE IF NOT EXISTS action_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (action_id) REFERENCES actions(id)
);
CREATE INDEX IF NOT EXISTS idx_action_logs_action ON action_logs(action_id);

CREATE TABLE IF NOT EXISTS action_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id TEXT NOT NULL,
  phase TEXT NOT NULL,
  rating TEXT NOT NULL,
  comment TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (action_id) REFERENCES actions(id)
);
CREATE INDEX IF NOT EXISTS idx_action_feedback_action ON action_feedback(action_id);
CREATE INDEX IF NOT EXISTS idx_briefings_date ON briefings(date);

CREATE TABLE IF NOT EXISTS interests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT,
  name        TEXT NOT NULL,
  description TEXT,
  keywords    TEXT,
  sort        TEXT DEFAULT 'relevance',
  item_limit  INTEGER DEFAULT 30,
  scope       TEXT DEFAULT 'all',
  enabled     INTEGER DEFAULT 1,
  scan_status TEXT DEFAULT 'pending',
  last_scan_at TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interest_matches (
  interest_id INTEGER,
  item_id     TEXT,
  relevance_score REAL,
  is_new      INTEGER DEFAULT 1,
  matched_at  TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (interest_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_interest_matches_interest ON interest_matches(interest_id);
CREATE INDEX IF NOT EXISTS idx_interest_matches_score ON interest_matches(interest_id, relevance_score DESC);

CREATE TABLE IF NOT EXISTS health_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp   TEXT DEFAULT (datetime('now')),
  platform    TEXT NOT NULL,
  old_status  TEXT,
  new_status  TEXT NOT NULL,
  message     TEXT,
  source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_log_platform ON health_log(platform);
CREATE INDEX IF NOT EXISTS idx_health_log_timestamp ON health_log(timestamp DESC);

-- v11.0: User authentication tables
CREATE TABLE IF NOT EXISTS users (
  id                    TEXT PRIMARY KEY,
  username              TEXT UNIQUE NOT NULL,
  email                 TEXT UNIQUE,
  password_hash         TEXT NOT NULL,
  role                  TEXT DEFAULT 'user',
  discord_bot_token_enc TEXT,
  email_verified        INTEGER DEFAULT 0,
  verification_code     TEXT,
  verification_code_expires TEXT,
  created_at            TEXT DEFAULT (datetime('now')),
  last_login_at         TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
  code        TEXT PRIMARY KEY,
  created_by  TEXT REFERENCES users(id),
  used_by     TEXT,
  max_uses    INTEGER DEFAULT 1,
  used_count  INTEGER DEFAULT 0,
  expires_at  TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id),
  token_type  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS user_profiles (
  user_id       TEXT PRIMARY KEY REFERENCES users(id),
  role          TEXT,
  interests     TEXT,
  tools         TEXT,
  manifest      TEXT,
  onboarding_completed INTEGER DEFAULT 0,
  created_at    TEXT DEFAULT (datetime('now')),
  updated_at    TEXT DEFAULT (datetime('now'))
);

-- v13.0: ASR 配额表,按 (user_id, date_cst) 分片,单用户时 user_id=0 占位
-- date_cst: 北京时间日期 'YYYY-MM-DD'(零点重置依赖此键)
-- seconds_used: 累计已消耗秒数(手动触发 bypass_quota=True 也计入)
-- updated_at: ISO 时间戳,用于调试
CREATE TABLE IF NOT EXISTS asr_usage (
  user_id       INTEGER NOT NULL DEFAULT 0,
  date_cst      TEXT NOT NULL,
  seconds_used  INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (user_id, date_cst)
);
CREATE INDEX IF NOT EXISTS idx_asr_usage_date ON asr_usage(date_cst);

-- v21.0 action-revival: 行动点生成每日配额表,按 (user_id, day_cst) 分片。
-- day_cst: 北京时间日期 'YYYY-MM-DD'(自然日零点重置依赖此键)。
-- count: 当日已发起的生成次数(item + cluster 合计;发起即计,失败/取消不退)。
-- 与 actions 行数解耦 —— 删除行动点不释放配额。
CREATE TABLE IF NOT EXISTS user_daily_generation (
  user_id     TEXT NOT NULL,
  day_cst     TEXT NOT NULL,
  count       INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (user_id, day_cst)
);
CREATE INDEX IF NOT EXISTS idx_user_daily_generation_day ON user_daily_generation(day_cst);

-- v15.0 event-aggregation: 3 new tables (PRD §5.12 / §5.13 / §5.14)
CREATE TABLE IF NOT EXISTS clusters (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  ai_title              TEXT,
  ai_summary            TEXT,
  ai_key_points         TEXT,
  why_read              TEXT,
  ai_summary_draft      TEXT,
  ai_title_draft        TEXT,
  ai_key_points_draft   TEXT,
  live_version          INTEGER NOT NULL DEFAULT 0,
  doc_count             INTEGER NOT NULL DEFAULT 0,
  platforms_json        TEXT,
  cover_url             TEXT,
  first_doc_at          TIMESTAMP NOT NULL,
  last_doc_at           TIMESTAMP,
  last_updated_at       TIMESTAMP,
  is_visible_in_feed    INTEGER NOT NULL DEFAULT 0,
  merged_into           INTEGER REFERENCES clusters(id),
  archived              INTEGER NOT NULL DEFAULT 0,
  prompt_version        TEXT,
  representative_vector BLOB,
  created_run_id        INTEGER,
  last_touched_run_id   INTEGER,
  published_run_id      INTEGER,
  published_at          TIMESTAMP,
  pending_is_visible_in_feed INTEGER,
  pending_summary_warnings_json TEXT,
  created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_clusters_visible_first_doc
  ON clusters(is_visible_in_feed, first_doc_at DESC);
CREATE INDEX IF NOT EXISTS idx_clusters_last_updated
  ON clusters(last_updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_clusters_merged_into
  ON clusters(merged_into) WHERE merged_into IS NOT NULL;

CREATE TABLE IF NOT EXISTS cluster_items (
  cluster_id        INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  item_id           TEXT    NOT NULL REFERENCES items(id)    ON DELETE CASCADE,
  rank_in_cluster   INTEGER,
  added_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  is_primary_source INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (cluster_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_items_item ON cluster_items(item_id);

CREATE TABLE IF NOT EXISTS cluster_status (
  user_id           TEXT    NOT NULL,
  cluster_id        INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  clicked_at        TIMESTAMP,
  starred_at        TIMESTAMP,
  last_seen_version INTEGER NOT NULL DEFAULT 0,
  feedback_kind     TEXT,
  feedback_at       TIMESTAMP,
  feedback_note     TEXT,
  PRIMARY KEY (user_id, cluster_id)
);

-- v15.0 generic settings KV store (feature flags / one-shot migration markers)
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- v22.0 subscription-config: 信源注册表(方案 B 单一真相源)。抓取管线改读此表;
-- item 挂 source_id 归属;停用/删除只停增量,存量与展示不变(软删 status=deleted)。
CREATE TABLE IF NOT EXISTS sources (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  platform     TEXT NOT NULL,            -- wechat_mp / x_user / rss / reddit / github_repo / bilibili_up
  source_key   TEXT NOT NULL,            -- 平台内唯一标识: channel_id / handle / feed URL / subreddit / owner/repo / uid
  display_name TEXT,
  status       TEXT NOT NULL DEFAULT 'active',  -- active/paused/pending/broken/not_fetched/deleted
  config_json  TEXT,                     -- per-source 参数(RSS slug / X 每轮条数等)
  origin       TEXT,                     -- seed_import / admin_add / reconcile_import
  validated_at TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_success_at TEXT,
  last_error TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE(platform, source_key)
);
"""


def get_conn():
    """Get a database connection, creating tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    # v8.0: Migrate old actions table (INTEGER PK → TEXT PK with new schema)
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'").fetchone()
        if row and 'user_intent' in (row[0] or ''):
            conn.executescript("DROP TABLE IF EXISTS actions; DROP TABLE IF EXISTS action_logs; DROP TABLE IF EXISTS action_feedback;")
    except Exception:
        pass
    conn.executescript(SCHEMA)
    # v22.0 subscription-config: item 归属到 sources 注册表(反查 (platform, source_key) 回填)
    try:
        conn.execute("ALTER TABLE items ADD COLUMN source_id INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # v22.0 subscription-config: per-source fetch failure tracking.
    try:
        conn.execute("ALTER TABLE sources ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE sources ADD COLUMN last_success_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE sources ADD COLUMN last_error TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    for _sql in (
        "CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status)",
        "CREATE INDEX IF NOT EXISTS idx_sources_platform ON sources(platform)",
        "CREATE INDEX IF NOT EXISTS idx_items_source_id ON items(source_id) WHERE source_id IS NOT NULL",
    ):
        try:
            conn.execute(_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # Add ai_category column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE items ADD COLUMN ai_category TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # v14 review: owner for private/manual submitted items
    try:
        conn.execute("ALTER TABLE items ADD COLUMN user_id TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_user ON items(user_id)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        status_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(item_status)").fetchall()
        }
        if 'user_id' in status_cols:
            conn.execute("""
                UPDATE items
                SET user_id = (
                    SELECT s.user_id
                    FROM item_status s
                    WHERE s.item_id = items.id AND s.starred_at IS NOT NULL
                    ORDER BY s.starred_at ASC
                    LIMIT 1
                )
                WHERE platform = 'manual'
                  AND user_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM item_status s
                    WHERE s.item_id = items.id
                      AND s.user_id IS NOT NULL
                      AND s.starred_at IS NOT NULL
                  )
            """)
        owner = conn.execute(
            "SELECT id FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at LIMIT 1"
        ).fetchone()
        if owner:
            conn.execute(
                "UPDATE items SET user_id = ? WHERE platform = 'manual' AND user_id IS NULL",
                (owner['id'],),
            )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # Index for ai_category (after column is guaranteed to exist)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ai_category ON items(ai_category)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # v12.1: Composite index for feed queries by category + time
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_category_fetched ON items(ai_category, fetched_at DESC)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # Add ai_keywords column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE items ADD COLUMN ai_keywords TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add ai_key_points column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE items ADD COLUMN ai_key_points TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add ai_relevance column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE items ADD COLUMN ai_relevance REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Add description column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE items ADD COLUMN description TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # v6.0.1: Add suggestion column to interests table
    try:
        conn.execute("ALTER TABLE interests ADD COLUMN suggestion TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # v9.0: Add Discord dispatch fields to actions table
    for col in ('discord_thread_id TEXT', 'discord_thread_url TEXT', 'dispatched_at TEXT'):
        try:
            conn.execute(f"ALTER TABLE actions ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v14 review: owner columns for multi-user isolation
    for table in ('actions', 'interests'):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_user ON {table}(user_id)")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    try:
        owner = conn.execute(
            "SELECT id FROM users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at LIMIT 1"
        ).fetchone()
        if owner:
            conn.execute("UPDATE actions SET user_id = ? WHERE user_id IS NULL", (owner['id'],))
            conn.execute("UPDATE interests SET user_id = ? WHERE user_id IS NULL", (owner['id'],))
            conn.commit()
    except sqlite3.OperationalError:
        pass
    # v12.0: Add content_type and ai_quality_score columns to items
    for col in ('content_type TEXT', 'ai_quality_score REAL'):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v13.1: AI retry/backoff state for summary/scoring enrichment.
    for col in (
        'ai_error_count INTEGER DEFAULT 0',
        'ai_last_error TEXT',
        'ai_last_error_at TEXT',
        'ai_retry_after TEXT',
        'ai_dimensions TEXT',
    ):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v11.1: Add email verification fields to users table
    for col in ('email_verified INTEGER DEFAULT 0', 'verification_code TEXT', 'verification_code_expires TEXT'):
        try:
            col_name = col.split()[0]
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            conn.commit()
            # Mark all existing users as verified
            if col_name == 'email_verified':
                conn.execute("UPDATE users SET email_verified = 1 WHERE email_verified IS NULL OR email_verified = 0")
                conn.commit()
                print("[db] email_verified column added, existing users marked as verified")
        except sqlite3.OperationalError:
            pass  # column already exists
    # v12.1: Add password reset fields to users table
    for col in ('reset_token TEXT', 'reset_token_expires TEXT'):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v21.0 action-revival: 每用户 Discord 派发目标频道(forum 或 text channel)
    for col in ('discord_channel_id TEXT',):
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v12.2: Twitter video ASR fields on items
    # asr_status enum: running|success|failed_download|failed_extract|
    #                  failed_upload|failed_asr|failed_empty|failed_summary
    # v12.3 新增: asr_segments(JSON array 含时间戳) / asr_text_cn(MiniMax 翻译中文)
    for col in (
        'asr_text TEXT',
        'asr_status TEXT',
        'asr_duration_sec INTEGER',
        'asr_cost_yuan REAL',
        'asr_attempted_at TEXT',
        'asr_failed_reason TEXT',
        "asr_provider TEXT DEFAULT 'doubao-seedasr-bigmodel'",
        'asr_segments TEXT',
        'asr_text_cn TEXT',
        'asr_segments_cn TEXT',
    ):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # Partial index: only non-null asr_status rows占索引空间
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_asr_status "
            "ON items(asr_status) WHERE asr_status IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # v15.0 event-aggregation: items / actions column extensions (PRD §5.15 / §5.16)
    for col in (
        'embedding BLOB',
        'embedding_provider TEXT',
        'cluster_id INTEGER',
        'cluster_locked INTEGER NOT NULL DEFAULT 0',
        'fetch_run_id INTEGER',
    ):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_cluster_id "
            "ON items(cluster_id) WHERE cluster_id IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_fetch_run_id "
            "ON items(fetch_run_id) WHERE fetch_run_id IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    for col in (
        'source_id TEXT',
        'cluster_version INTEGER',
        'is_stale INTEGER NOT NULL DEFAULT 0',
        'steps TEXT',  # v21.0: 结构化行动点(人看),与自包含 prompt(机器执行)分离
    ):
        try:
            conn.execute(f"ALTER TABLE actions ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # v15.0 default feature flag (R9.1 冷启动期 enabled=false)
    # NOTE: actions truncate (PRD §13.6 S1) is NOT auto-run here — destructive,
    # call `python scripts/truncate_actions_v15.py` manually before flipping the flag.
    try:
        existing = conn.execute(
            "SELECT value FROM settings WHERE key = 'event_aggregation_ready'"
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('event_aggregation_ready', 'false')"
            )
            conn.commit()
    except sqlite3.OperationalError:
        pass
    # v15.1 event-aggregation V2: schema diff (PRD §5.17)
    # - clusters.unique_source_count: V2 来源去重新口径，可见门槛改成 >=2
    # - clusters.last_summary_warnings_json: Stage 4 warnings 审计
    # - clusters.event_embedding: 基于 ai_title + ai_summary + ai_key_points 的稳定召回向量
    # - cluster_items.source_identity: canonical_url -> normalized_url -> ... 优先级去重
    # - cluster_items.join_decision_id: 关联 cluster_judge_log.id
    for col in (
        'unique_source_count INTEGER NOT NULL DEFAULT 0',
        'last_summary_warnings_json TEXT',
        'event_embedding BLOB',
        'created_run_id INTEGER',
        'last_touched_run_id INTEGER',
        'published_run_id INTEGER',
        'published_at TIMESTAMP',
        'pending_is_visible_in_feed INTEGER',
        'pending_summary_warnings_json TEXT',
        'why_read TEXT',
    ):
        try:
            conn.execute(f"ALTER TABLE clusters ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    try:
        conn.execute(
            """UPDATE clusters
                  SET published_at = COALESCE(last_updated_at, created_at),
                      published_run_id = COALESCE(published_run_id, 0)
                WHERE is_visible_in_feed = 1
                  AND published_at IS NULL"""
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clusters_published "
            "ON clusters(is_visible_in_feed, published_at DESC)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    for col in (
        'source_identity TEXT',
        'join_decision_id TEXT',
    ):
        try:
            conn.execute(f"ALTER TABLE cluster_items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # event-pipeline-v2 / Stage A: items 表 7 列扩展
    # 设计文档: docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5
    # 行为: 每条 enriched item 由 Stage A 写 BGE-M3 × aikw embedding + canonical_url
    for col in (
        'embedding_model TEXT',
        'embedding_input_variant TEXT',
        'embedding_generated_at TEXT',
        'canonical_url TEXT',
        'stage_a_state TEXT',
        'stage_a_failed_at TEXT',
    ):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_stage_a_state "
            "ON items(stage_a_state) WHERE stage_a_state IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # v18.1 event-library-actions: per-user cluster favorite state.
    try:
        conn.execute("ALTER TABLE cluster_status ADD COLUMN starred_at TIMESTAMP")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # v25.0 highlights-quality: per-user cluster quality feedback (只落库不改排序).
    for _fb_sql in (
        "ALTER TABLE cluster_status ADD COLUMN feedback_kind TEXT",
        "ALTER TABLE cluster_status ADD COLUMN feedback_at TIMESTAMP",
        "ALTER TABLE cluster_status ADD COLUMN feedback_note TEXT",
    ):
        try:
            conn.execute(_fb_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    for idx_sql in (
        "CREATE INDEX IF NOT EXISTS idx_cluster_status_user_clicked "
        "ON cluster_status(user_id, clicked_at DESC) WHERE clicked_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_cluster_status_user_starred "
        "ON cluster_status(user_id, starred_at DESC) WHERE starred_at IS NOT NULL",
    ):
        try:
            conn.execute(idx_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    # event-pipeline-v2 simplified / Stage Z + Stage P: clusters_v2 / cluster_items_v2 / cluster_p_log
    # 设计文档: docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5.5
    # 决策: v1 / v2 直接替换路线下用新表，v1 旧表（clusters / cluster_items）保留不动等切换时一起 drop
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS clusters_v2 (
                 id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                 centroid               BLOB,
                 dominant_category      TEXT,
                 event_summary          TEXT,
                 event_certainty        TEXT,
                 member_count           INTEGER NOT NULL DEFAULT 0,
                 created_at             TEXT NOT NULL DEFAULT (datetime('now')),
                 last_member_added_at   TEXT,
                 stage_p_state          TEXT,
                 stage_p_run_at         TEXT,
                 stage_p_failed_reason  TEXT
               )"""
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clusters_v2_stage_p_state "
            "ON clusters_v2(stage_p_state) WHERE stage_p_state IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cluster_items_v2 (
                 cluster_id      INTEGER NOT NULL REFERENCES clusters_v2(id) ON DELETE CASCADE,
                 item_id         TEXT    NOT NULL REFERENCES items(id)        ON DELETE CASCADE,
                 added_at        TEXT NOT NULL DEFAULT (datetime('now')),
                 joined_cosine   REAL,
                 removed_at      TEXT,
                 removed_reason  TEXT,
                 PRIMARY KEY (cluster_id, item_id)
               )"""
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cluster_items_v2_item ON cluster_items_v2(item_id)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cluster_items_v2_visible "
            "ON cluster_items_v2(cluster_id) WHERE removed_at IS NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cluster_p_log (
                 id            INTEGER PRIMARY KEY AUTOINCREMENT,
                 cluster_id    INTEGER NOT NULL,
                 item_id       TEXT,
                 action        TEXT NOT NULL,
                 reason        TEXT,
                 llm_model     TEXT,
                 raw_response  TEXT,
                 created_at    TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cluster_p_log_cluster ON cluster_p_log(cluster_id)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # v15.1 cluster_judge_log: Stage 2 LLM 决策完整审计
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cluster_judge_log (
                 id                        INTEGER PRIMARY KEY AUTOINCREMENT,
                 item_id                   TEXT NOT NULL,
                 candidate_cluster_ids     TEXT,
                 llm_input_tokens          INTEGER,
                 llm_output_tokens         INTEGER,
                 matches_json              TEXT,
                 selected_cluster_id       INTEGER,
                 selection_reason          TEXT,
                 possible_merge_candidates TEXT,
                 decision_model            TEXT NOT NULL,
                 created_at                TEXT NOT NULL DEFAULT (datetime('now'))
               )"""
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cluster_judge_log_item "
            "ON cluster_judge_log(item_id)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cluster_judge_log_selected "
            "ON cluster_judge_log(selected_cluster_id) "
            "WHERE selected_cluster_id IS NOT NULL"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # v16 classification v4.0: L1/L2 multi-tag, ai_extracted JSON, visible filter
    # See docs/优化/2026-04-29-推荐tab分类体系.md
    for col in (
        "ai_categories TEXT",            # JSON array, 1-3 个 L1 id
        "ai_subcategories TEXT",         # JSON array, L2 id 不限
        "multi_l1_reason TEXT",          # len(ai_categories)>1 时填
        "ai_extracted TEXT",             # JSON object: {skills, models, event_card, ...}
        "visible INTEGER DEFAULT 1",     # 过滤层标记 (1=可见, 0=非主题隐藏但留训练数据)
    ):
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # Indexes for v4.0 queries
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_visible ON items(visible)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def bump_cluster_version_and_stale_actions(conn, cluster_id, new_version):
    """Atomically bump cluster live_version and mark all older cluster-sourced actions stale.

    Called by summary_writer.regenerate_and_swap() inside a transaction.
    Feature Spec R6.2: "cluster live_version bump 后老 action 自动 stale"
    """
    conn.execute(
        "UPDATE clusters SET live_version = ? WHERE id = ?",
        (new_version, cluster_id),
    )
    conn.execute(
        """UPDATE actions SET is_stale = 1
           WHERE source_type = 'cluster'
             AND source_id = ?
             AND (cluster_version IS NULL OR cluster_version < ?)
             AND is_stale = 0""",
        (str(cluster_id), new_version),
    )
    conn.commit()


# v22.0 subscription-config: 停用兜底——这些状态的源,其新 item 不入库(存量不动)。
_SOURCE_DROP_STATUSES = frozenset({'paused', 'deleted', 'broken'})


def _source_config_dict(raw_cfg):
    if isinstance(raw_cfg, dict):
        return raw_cfg
    if raw_cfg:
        try:
            parsed = json.loads(raw_cfg)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            return {}
    return {}


def build_source_index_from_rows(rows):
    """把 sources 注册表载入内存,供 ingest 时按 (platform, item.source) 反查。

    返回 dict, 各平台一个子 map: 反查键 → (source_id, status)。
    反查键因平台而异(item.source 是平台内子源串, 与 sources.source_key 语义不同):
      rss    : config_json.slug   (item.source = 'feed:{slug}')
      reddit : source_key(sub)    (item.source = 'r/{sub}')
      github : source_key(owner/repo) (item.source = 'awesome:{owner/repo}')
      wechat RSS : source_key(feed URL) (item.source = 'wechat:{url}')
      wechat Lingowhale : source_key(channel_id) (item.source = 'lingowhale:{channel_id}')
      x_user : source_key(handle)
      bili   : source_key(uid)
    """
    idx = {
        'rss_by_slug': {}, 'reddit_by_key': {}, 'github_by_key': {},
        'wechat_by_url': {}, 'wechat_by_channel_id': {},
        'x_by_handle': {}, 'bili_by_uid': {},
    }
    for row in rows:
        sid, plat, key, status, cfg = (
            row['id'], row['platform'], row['source_key'], row['status'], row['config_json'])
        if plat == 'rss':
            slug = _source_config_dict(cfg).get('slug')
            if slug:
                idx['rss_by_slug'][slug] = (sid, status)
        elif plat == 'reddit':
            idx['reddit_by_key'][key] = (sid, status)
        elif plat == 'github_repo':
            idx['github_by_key'][key] = (sid, status)
        elif plat == 'wechat_mp':
            backend = _source_config_dict(cfg).get('backend')
            is_url = isinstance(key, str) and key.startswith(('http://', 'https://'))
            if backend == 'lingowhale' or not is_url:
                idx['wechat_by_channel_id'][key] = (sid, status)
            else:
                idx['wechat_by_url'][key] = (sid, status)
        elif plat == 'x_user':
            idx['x_by_handle'][key] = (sid, status)
        elif plat == 'bilibili_up':
            idx['bili_by_uid'][key] = (sid, status)
    return idx


def load_source_index(conn):
    return build_source_index_from_rows(conn.execute(
        "SELECT id, platform, source_key, status, config_json FROM sources"))


def normalize_active_source_row(row):
    return {
        'id': row['id'],
        'source_key': row['source_key'],
        'display_name': row['display_name'],
        'config_json': _source_config_dict(row['config_json']),
    }


def list_active_sources(conn, platform):
    """Return sources eligible for scheduled fetch and broken recovery."""
    if platform == 'x_user':
        rows = conn.execute(
            """SELECT id, source_key, display_name, config_json
               FROM sources
               WHERE platform = ? AND status IN ('active', 'broken', 'not_fetched')
               ORDER BY id""",
            (platform,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, source_key, display_name, config_json
               FROM sources
               WHERE platform = ? AND status IN ('active', 'broken')
               ORDER BY id""",
            (platform,),
        ).fetchall()
    return [normalize_active_source_row(row) for row in rows]


def _broken_after_threshold():
    try:
        with open(os.path.join(BASE, 'config', 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        raw = (cfg.get('sources') or {}).get('broken_after_failures', 5)
        value = int(raw)
        return value if value > 0 else 5
    except Exception:
        return 5


def record_source_fetch_result(conn, source_id, *, ok, error=None, broken_after=5):
    """Record one source fetch result without interrupting the fetch pipeline."""
    try:
        if source_id is None:
            return
        row = conn.execute(
            "SELECT status, consecutive_failures FROM sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if row is None:
            return
        status = row['status']
        if status not in {'active', 'broken', 'not_fetched'}:
            return

        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if ok:
            new_status = 'active'
            conn.execute(
                """UPDATE sources
                      SET status = ?,
                          consecutive_failures = 0,
                          last_success_at = ?,
                          last_error = NULL,
                          updated_at = ?
                    WHERE id = ?""",
                (new_status, now, now, source_id),
            )
            conn.commit()
            return

        try:
            threshold = int(broken_after)
        except (TypeError, ValueError):
            threshold = 5
        if threshold <= 0:
            threshold = 5
        current_failures = int(row['consecutive_failures'] or 0)
        failures = current_failures + 1
        new_status = 'broken' if failures >= threshold else status
        last_error = None if error is None else str(error)[:500]
        conn.execute(
            """UPDATE sources
                  SET status = ?,
                      consecutive_failures = ?,
                      last_error = ?,
                      updated_at = ?
                WHERE id = ?""",
            (new_status, failures, last_error, now, source_id),
        )
        conn.commit()
    except Exception as exc:
        print(f"[warn] failed to record source fetch result for source_id={source_id}: {exc}")


def resolve_source(index, platform, source, *, channel_id=None, handle=None, uid=None):
    """把 item 的 (platform, source) 映射到 (source_id, status)。

    算法源/榜单(twitter following/for-you、hackernews、bili hot/rank、github trending、
    xiaohongshu、waytoagi)不在注册表 → 返回 (None, None), 表示不归属、不过滤。
    """
    if not index:
        return (None, None)
    if platform == 'rss' and isinstance(source, str) and source.startswith('feed:'):
        return index['rss_by_slug'].get(source[len('feed:'):], (None, None))
    if platform == 'reddit' and isinstance(source, str) and source.startswith('r/'):
        return index['reddit_by_key'].get(source[len('r/'):], (None, None))
    if platform == 'github' and isinstance(source, str) and source.startswith('awesome:'):
        return index['github_by_key'].get(source[len('awesome:'):], (None, None))
    if platform == 'lingowhale':
        if channel_id:
            return index.get('wechat_by_channel_id', {}).get(channel_id, (None, None))
        if isinstance(source, str) and source.startswith('lingowhale:'):
            return index.get('wechat_by_channel_id', {}).get(
                source[len('lingowhale:'):], (None, None))
        if isinstance(source, str) and source.startswith('wechat:'):
            return index.get('wechat_by_url', {}).get(source[len('wechat:'):], (None, None))
    if platform == 'twitter' and isinstance(source, str) and source.startswith('user:'):
        return index['x_by_handle'].get(source[len('user:'):], (None, None))
    if platform == 'x_user' and handle:
        return index['x_by_handle'].get(handle, (None, None))
    if platform == 'bilibili' and uid:
        return index['bili_by_uid'].get(uid, (None, None))
    return (None, None)


def upsert_item(conn, item_dict, source_index=None):
    """Insert or update an item. On conflict, update metrics and detail.

    v22.0: 若传入 source_index(load_source_index 产出), 则:
      - 反查并回填 items.source_id(算法源为 None);
      - 若源状态属停用集(paused/deleted/broken), 丢弃本 item 不入库, 返回 'dropped'。
    不传 source_index 时行为与旧版一致(source_id 取 item_dict 里的值, 通常 None)。
    """
    if source_index is not None:
        sid, status = resolve_source(
            source_index, item_dict.get('platform'), item_dict.get('source'),
            channel_id=item_dict.get('channel_id'),
            handle=item_dict.get('author_handle') or item_dict.get('handle'),
            uid=item_dict.get('author_id'),
        )
        if status in _SOURCE_DROP_STATUSES:
            return 'dropped'
        item_dict = dict(item_dict)
        item_dict['source_id'] = sid
    cols = ['id', 'user_id', 'platform', 'source', 'source_id', 'fetch_run_id', 'title', 'content', 'author_name',
            'author_id', 'author_avatar', 'url', 'cover_url', 'media_json',
            'metrics_json', 'tags_json', 'lang', 'detail_json', 'comments_json',
            'ai_summary', 'ai_key_points', 'relevance_score', 'fetched_at', 'published_at']
    vals = [item_dict.get(c) for c in cols]
    placeholders = ','.join(['?'] * len(cols))
    col_names = ','.join(cols)
    run_id = item_dict.get('fetch_run_id')
    existed_before = None
    run_exists = False
    if run_id is not None:
        run_exists = conn.execute(
            "SELECT 1 FROM fetch_runs WHERE id = ?",
            (run_id,),
        ).fetchone() is not None
        existed_before = conn.execute(
            "SELECT 1 FROM items WHERE id = ?",
            (item_dict.get('id'),),
        ).fetchone() is not None
    # On conflict: update metrics, detail, comments, cover, source (data that may change)
    conn.execute(f"""
        INSERT INTO items ({col_names}) VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET
            content = CASE WHEN length(excluded.content) > length(COALESCE(content,'')) THEN excluded.content ELSE content END,
            url = CASE WHEN excluded.url != '' THEN excluded.url ELSE url END,
            ai_summary = COALESCE(excluded.ai_summary, ai_summary),
            ai_key_points = COALESCE(excluded.ai_key_points, ai_key_points),
            metrics_json = excluded.metrics_json,
            detail_json = COALESCE(excluded.detail_json, detail_json),
            comments_json = excluded.comments_json,
            cover_url = COALESCE(excluded.cover_url, cover_url),
            author_name = COALESCE(NULLIF(excluded.author_name, ''), author_name),
            source = COALESCE(NULLIF(excluded.source, ''), source),
            fetch_run_id = COALESCE(excluded.fetch_run_id, fetch_run_id),
            fetched_at = CASE
                WHEN excluded.fetch_run_id IS NOT NULL THEN excluded.fetched_at
                ELSE fetched_at
            END
    """, vals)
    if run_id is not None and run_exists:
        was_inserted = 0 if existed_before else 1
        conn.execute(
            """INSERT INTO fetch_run_items
                 (run_id, item_id, platform, source, was_inserted)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(run_id, item_id) DO UPDATE SET
                 platform = excluded.platform,
                 source = excluded.source,
                 was_inserted = CASE
                   WHEN fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1 THEN 1
                   ELSE 0
                 END,
                 recorded_at = datetime('now')""",
            (
                run_id,
                item_dict.get('id'),
                item_dict.get('platform'),
                item_dict.get('source'),
                was_inserted,
            ),
        )


def batch_upsert(conn, items, fetch_run_id=None):
    """Upsert a list of items in a single transaction."""
    for item in items:
        if fetch_run_id is not None:
            item = dict(item)
            item['fetch_run_id'] = fetch_run_id
        upsert_item(conn, item)
    conn.commit()
    return len(items)


_STATUS_COLUMNS = {
    'clicked': 'clicked_at',
    'starred': 'starred_at',
    'hidden': 'hidden_at',
    'read': 'read_at',
}

def set_status(conn, item_id, action, force=False, user_id=None):
    """Set a status flag on an item. action: 'clicked' | 'starred' | 'hidden' | 'read'.

    For 'starred' and 'hidden', toggles the value (set if null, clear if already set).
    For 'clicked' and 'read', always sets the timestamp.
    If force=True, always sets the timestamp (no toggle).
    user_id: if provided, uses composite PK (user_id, item_id). If None, legacy single-key mode.
    """
    col = _STATUS_COLUMNS.get(action)
    if col is None:
        raise ValueError(f"Invalid action: {action!r}. Must be one of {list(_STATUS_COLUMNS)}")
    now = datetime.now().isoformat()

    # Check if table has user_id column
    has_user_id = _check_item_status_has_user_id(conn)

    # item_status 表 PK 是复合 (user_id, item_id)。匿名调用(user_id=None)时没有归属,
    # 无法生成匹配的 ON CONFLICT 子句,静默跳过(前端 starred/clicked 在匿名态本来就无处存)。
    if has_user_id and not user_id:
        return

    if has_user_id:
        pk_where = "user_id = ? AND item_id = ?"
        pk_params = (user_id, item_id)
        insert_cols = f"user_id, item_id, {col}"
        insert_vals = "?, ?, ?"
        insert_params = (user_id, item_id, now, now)
        conflict_target = "user_id, item_id"
    else:
        pk_where = "item_id = ?"
        pk_params = (item_id,)
        insert_cols = f"item_id, {col}"
        insert_vals = "?, ?"
        insert_params = (item_id, now, now)
        conflict_target = "item_id"

    if action in ('starred', 'hidden') and not force:
        row = conn.execute(
            f"SELECT {col} FROM item_status WHERE {pk_where}", pk_params
        ).fetchone()
        if row and row[col]:
            conn.execute(
                f"UPDATE item_status SET {col} = NULL WHERE {pk_where}", pk_params
            )
        else:
            conn.execute(f"""
                INSERT INTO item_status ({insert_cols}) VALUES ({insert_vals})
                ON CONFLICT({conflict_target}) DO UPDATE SET {col} = ?
            """, insert_params)
    else:
        conn.execute(f"""
            INSERT INTO item_status ({insert_cols}) VALUES ({insert_vals})
            ON CONFLICT({conflict_target}) DO UPDATE SET {col} = ?
        """, insert_params)
    conn.commit()


def _add_manual_visibility(where, params, *, public_only=False,
                           manual_owner_user_id=None, alias='i'):
    """Append visibility rules for private manual submissions."""
    if public_only:
        where.append(f"{alias}.platform != 'manual'")
    elif manual_owner_user_id:
        where.append(f"({alias}.platform != 'manual' OR {alias}.user_id = ?)")
        params.append(manual_owner_user_id)


def github_min_stars_for_display(default=50):
    """Return the configured minimum stars for GitHub repo display."""
    try:
        with open(os.path.join(BASE, 'config', 'config.json'), encoding='utf-8') as f:
            cfg = json.load(f)
        raw = (cfg.get('display') or {}).get('github_min_stars', default)
        return max(0, int(raw))
    except Exception:
        return default


def _add_display_visibility(where, params, *, alias='i'):
    """Append display-only rules that should not mutate stored feed data.

    v16.0 PRD §4.9.5 S11: 历史 source LIKE 'search:%' 数据 DB 留底但前端任何
    查询路径都不展示。SQL 必须参数化（防 SQL injection；feedback_qa_assertion_depth）。
    """
    # v16.0: 排除历史 keyword search 抓取数据（Twitter/B站/GitHub search:keyword）
    where.append(f"({alias}.source IS NULL OR {alias}.source NOT LIKE ?)")
    params.append('search:%')
    # v18.2: 信息 tab 暂不展示 X 书签，DB 原始数据保留。
    where.append(f"({alias}.platform != ? OR COALESCE({alias}.source, '') != ?)")
    params.extend(['twitter', 'bookmarks'])

    min_stars = github_min_stars_for_display()
    if min_stars <= 0:
        return
    where.append(
        f"""({alias}.platform != 'github' OR (
            json_valid({alias}.metrics_json)
            AND CAST(COALESCE(json_extract({alias}.metrics_json, '$.stars'), 0) AS INTEGER) >= ?
        ))"""
    )
    params.append(min_stars)


UNCATEGORIZED_SENTINEL = '__uncategorized__'  # BF-0512-6: L1 「未分类」pill 占位符


def _add_ai_relevance_filter(where, params, *, alias='i'):
    """v18.0 nav-merge: 强制 AI 相关性过滤（信息 tab 复用 query_feed_platforms）。

    PRD §Spec-2 锁定口径（D3 决策）：
        (ai_category IS NOT NULL AND ai_category != 'other')
     OR (ai_categories IS NOT NULL AND ai_categories NOT IN ('[]','null','"null"'))

    Why OR：
    - 单字段 ai_category 严格会丢小红书/Reddit/Twitter 长尾的 multi-tag 数据
    - v15+ multi-tag 字段 ai_categories 是后续主力，但老数据只有单字段 ai_category
    - OR 口径相比单字段严格只多 ~3% 留存，对长尾平台收益明显，向前兼容

    实证（remote_poc 全量，2026-05-15）：60,436 → 41,673 (69%)
    """
    where.append(
        f"((({alias}.ai_category IS NOT NULL AND {alias}.ai_category != 'other')"
        f" OR ({alias}.ai_categories IS NOT NULL"
        f" AND {alias}.ai_categories NOT IN ('[]', 'null', '\"null\"'))))"
    )


def _add_category_filter(where, params, category, *, alias='i'):
    """v16.0: ai_categories JSON array 任意元素匹配 category 的过滤条件。

    用于频道页 L1 pill（GitHub/Reddit/RSS/HN/WayToAGI/Manual section）。
    SQLite JSON1: json_each + EXISTS 实现 array contains。

    BF-0512-6: 当 category=='__uncategorized__'（UNCATEGORIZED_SENTINEL）时，
    过滤 ai_categories IS NULL 的 item — 让用户能看到 v4.0+ 之前 / enrich 失败的
    历史数据「全部」pill 与 L1 pill 加和不对账的 NULL 部分。

    Args:
        category: L1 分类 id（products/efficiency_tools/coding/...）；
                  None/空字符串 → 不加过滤；
                  '__uncategorized__' → 过滤 ai_categories IS NULL。
    """
    if not category:
        return
    # BF-0512-6: 「未分类」pill - 选 ai_categories IS NULL 的 item
    if category == UNCATEGORIZED_SENTINEL:
        where.append(f"{alias}.ai_categories IS NULL")
        return
    where.append(
        f"""EXISTS (
            SELECT 1 FROM json_each({alias}.ai_categories) WHERE value = ?
        )"""
    )
    params.append(category)


def query_feed(conn, platform=None, source=None, unread=False,
               starred=False, clicked=False, limit=50, offset=0,
               search=None, user_id=None, public_only=False,
               manual_owner_user_id=None):
    """Query items with optional filters. user_id scopes item_status to that user."""
    where, params = [], []

    # Build join condition based on user_id
    join_cond, join_params = _item_status_join(conn, user_id)
    params.extend(join_params)

    if search:
        like = f'%{search}%'
        where.append("(i.title LIKE ? OR i.content LIKE ? OR i.author_name LIKE ? OR i.ai_summary LIKE ? OR i.ai_keywords LIKE ?)")
        params += [like, like, like, like, like]
    if platform:
        where.append("i.platform = ?")
        params.append(platform)
    _add_manual_visibility(
        where, params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(where, params)
    if source:
        where.append("i.source = ?")
        params.append(source)
    if unread:
        where.append("""
            (s.item_id IS NULL OR
             (s.clicked_at IS NULL AND s.hidden_at IS NULL))
        """)
    if starred:
        where.append("s.starred_at IS NOT NULL")
    if clicked:
        where.append("s.clicked_at IS NOT NULL")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # I2: History view sorts by clicked_at descending
    order = "s.clicked_at DESC" if clicked else "i.fetched_at DESC, i.published_at DESC"
    limit_sql = "LIMIT ? OFFSET ?" if limit > 0 else ("OFFSET ?" if offset > 0 else "")
    # Exclude heavy columns (content, detail_json, comments_json) from list queries
    # description is included as lightweight fallback when ai_summary is empty
    cols = """i.id, i.platform, i.source, i.title, i.author_name, i.author_id,
              i.author_avatar, i.url, i.cover_url, i.media_json, i.metrics_json,
              i.tags_json, i.lang, i.description, i.ai_summary, i.ai_key_points,
              i.ai_category, i.ai_keywords, i.relevance_score, i.fetched_at,
              i.published_at, i.created_at, s.read_at, s.clicked_at, s.starred_at,
              s.hidden_at"""
    sql = f"""
        SELECT {cols}
        FROM items i
        {join_cond}
        {where_sql}
        ORDER BY {order}
        {limit_sql}
    """
    if limit > 0:
        params += [limit, offset]
    elif offset > 0:
        params += [offset]
    rows = conn.execute(sql, params).fetchall()
    return [_normalize_item_category(dict(r)) for r in rows]


def query_feed_sections(conn, user_id=None, per_category=50, public_only=False,
                        manual_owner_user_id=None):
    """Query items with lightweight fields, grouped by ai_category.

    per_category: max items per category (default 50). None for unlimited.
    """
    cols = """i.id, i.platform, i.source, i.title, i.cover_url, i.ai_summary,
              i.ai_keywords, i.ai_category, i.relevance_score, i.fetched_at,
              i.published_at, i.author_name, i.metrics_json,
              i.content_type, i.ai_quality_score,
              i.ai_categories, i.ai_subcategories, i.multi_l1_reason,
              i.ai_extracted, i.visible,
              s.clicked_at, s.starred_at"""
    params = []
    join_cond, join_params = _item_status_join(conn, user_id)
    params.extend(join_params)
    where, where_params = [], []
    _add_manual_visibility(
        where, where_params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(where, where_params)
    # v4.0 visible filter: hide items the LLM marked as off-topic (visible=0).
    where.append("i.visible = 1")
    # v18.0 Spec-2.5（rev1, 2026-05-15）: 与 query_feed_platforms 同一份双字段
    # OR AI 过滤口径，保证「按频道」/「按分类」两个视角看到同一批数据。
    # 取消 v4.0 历史的 `ai_categories IS NOT NULL` 单字段严格过滤——单字段
    # 严格会丢长尾 multi-tag 数据，且与 platforms 入口口径不一致导致用户在
    # 两个视角间切换看到的总数差很多。OR 口径已通过 _add_ai_relevance_filter
    # 同步生效，分组键自动 fallback 到 _uncategorized。
    _add_ai_relevance_filter(where, params)
    params.extend(where_params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT {cols}
        FROM items i
        {join_cond}
        {where_sql}
        ORDER BY i.fetched_at DESC, i.relevance_score DESC
    """
    rows = conn.execute(sql, params).fetchall()
    sections = {}
    cat_counts = {}
    for r in rows:
        d = _normalize_item_category(dict(r))
        # v18.0 Spec-2.5: 分组优先 ai_categories[0]（multi-tag 主分类），
        # 缺失时 fallback 到 ai_category 单字段（OR 过滤后允许此分支），
        # 仍然没有就归到 _uncategorized（理论上不会出现，但兜底防 KeyError）。
        cats = d.get('ai_categories')
        primary_cat = cats[0] if isinstance(cats, list) and cats else None
        if not primary_cat:
            single = d.get('ai_category')
            primary_cat = single if (single and single != 'other') else None
        cat = primary_cat or '_uncategorized'
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if per_category is None or len(sections.get(cat, [])) < per_category:
            if cat not in sections:
                sections[cat] = []
            sections[cat].append(d)
    return sections, cat_counts


def query_feed_by_category(conn, category, user_id=None, keyword=None,
                           subcategory=None,
                           public_only=False, manual_owner_user_id=None):
    """Query all items for a specific ai_category (ranking + pagination done in Python).

    keyword: optional substring filter applied to title / ai_summary / ai_keywords
    via SQL LIKE so 一个 category 下全库匹配，而不是仅匹配前端已加载的批次。

    subcategory (v4.0): optional L2 id filter against ai_subcategories JSON array
    (LIKE '%"<l2_id>"%'). Old items without ai_subcategories naturally drop out.
    """
    cols = """i.id, i.platform, i.source, i.title, i.cover_url, i.ai_summary,
              i.ai_keywords, i.ai_category, i.relevance_score, i.fetched_at,
              i.published_at, i.author_name, i.metrics_json,
              i.content_type, i.ai_quality_score,
              i.ai_categories, i.ai_subcategories, i.multi_l1_reason,
              i.ai_extracted, i.visible,
              s.clicked_at, s.starred_at"""
    params = []
    join_cond, join_params = _item_status_join(conn, user_id)
    params.extend(join_params)
    if category == '_uncategorized':
        where = "WHERE (i.ai_category IS NULL OR i.ai_category = '')"
    else:
        category_ids = expand_query_categories(category)
        placeholders = ",".join(["?"] * len(category_ids))
        where = f"WHERE i.ai_category IN ({placeholders})"
        params.extend(category_ids)
    if keyword:
        kw_like = f"%{keyword}%"
        where += " AND (i.title LIKE ? OR i.ai_summary LIKE ? OR i.ai_keywords LIKE ?)"
        params.extend([kw_like, kw_like, kw_like])
    if subcategory:
        # JSON array LIKE match: ai_subcategories stored as e.g. '["coding_tool","other"]'
        sub_like = f'%"{subcategory}"%'
        where += " AND i.ai_subcategories LIKE ?"
        params.append(sub_like)
    extra_where, extra_params = [], []
    _add_manual_visibility(
        extra_where, extra_params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(extra_where, extra_params)
    # v4.0 visibility filter + hide pre-v4 items (not yet re-enriched)
    extra_where.append("i.visible = 1")
    extra_where.append("i.ai_categories IS NOT NULL")
    if extra_where:
        where += " AND " + " AND ".join(extra_where)
        params.extend(extra_params)
    sql = f"""
        SELECT {cols}
        FROM items i
        {join_cond}
        {where}
        ORDER BY i.fetched_at DESC, i.id DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [_normalize_item_category(dict(r)) for r in rows]


def query_feed_platforms(conn, user_id=None, per_platform=50, public_only=False,
                         manual_owner_user_id=None):
    """Query items grouped by platform, with per-platform limit and total counts.

    v18.0 nav-merge: 信息 tab 复用本函数；强制 AI 相关性过滤（D3）。
    """
    cols = """i.id, i.platform, i.source, i.title, i.cover_url, i.ai_summary,
              i.ai_keywords, i.ai_category, i.relevance_score, i.fetched_at,
              i.author_name, i.metrics_json,
              s.clicked_at, s.starred_at"""
    params = []
    join_cond, join_params = _item_status_join(conn, user_id)
    params.extend(join_params)
    where, where_params = [], []
    _add_manual_visibility(
        where, where_params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(where, where_params)
    # v18.0 PRD §Spec-2: 强制 AI 相关性过滤（OR 双字段口径）
    _add_ai_relevance_filter(where, where_params)
    params.extend(where_params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT {cols}
        FROM items i
        {join_cond}
        {where_sql}
        ORDER BY i.fetched_at DESC, i.relevance_score DESC, i.id DESC
    """
    rows = conn.execute(sql, params).fetchall()
    sections = {}
    platform_counts = {}
    source_counts = {}  # { platform: { source: count } }
    for r in rows:
        d = _normalize_item_category(dict(r))
        plat = d.get('platform') or '_unknown'
        platform_counts[plat] = platform_counts.get(plat, 0) + 1
        # Track source distribution per platform
        src = d.get('source') or ''
        if plat not in source_counts:
            source_counts[plat] = {}
        source_counts[plat][src] = source_counts[plat].get(src, 0) + 1
        if per_platform is None or len(sections.get(plat, [])) < per_platform:
            if plat not in sections:
                sections[plat] = []
            sections[plat].append(d)
    return sections, platform_counts, source_counts


def _feed_by_platform_where(platform, *, source=None, group=None, category=None,
                            public_only=False, manual_owner_user_id=None):
    where = "WHERE i.platform = ?"
    params = [platform]
    extra_where, extra_params = [], []
    _add_manual_visibility(
        extra_where, extra_params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(extra_where, extra_params)
    # v4.0 visibility filter
    extra_where.append("i.visible = 1")
    # v18.0 nav-merge: 故意不在 query_feed_by_platform 加 AI 过滤
    # 信息 tab 入口（query_feed_platforms / category_counts）已强制过滤；
    # section 内 source pill / 未分类 pill 仍按 v16 逻辑放行 NULL 历史数据，
    # 维持 BF-0512-2/4/6 已上线行为（D7 锁定）。代价：nav 角标与子 pill 加和
    # 可能略偏，与 v16 现状一致，可接受。
    # v16.0 BF-0512-2+4: 仅 L1 category 维度切换时 require ai_categories
    # (source/group 维度切换 / 无 category 时放行 NULL，对齐顶层 query_feed_platforms)
    # BF-0512-6: 「未分类」pill 例外 - category=UNCATEGORIZED_SENTINEL 就是要 NULL
    if category and category != UNCATEGORIZED_SENTINEL:
        extra_where.append("i.ai_categories IS NOT NULL")
    # v16.0 L1 category filter（数组任意元素匹配；含 UNCATEGORIZED 分支）
    _add_category_filter(extra_where, extra_params, category)
    if extra_where:
        where += " AND " + " AND ".join(extra_where)
        params.extend(extra_params)
    if source:
        where += " AND i.source = ?"
        params.append(source)
    if group:
        # BF-0419-11: "未分组" 同时匹配 detail_json.group='未分组'/NULL/'独立频道'
        # (历史代码把独立订阅频道字面打成"独立频道",109 条,合并显示更直观)
        if group == '未分组':
            where += " AND (json_extract(i.detail_json, '$.group') IN ('未分组','独立频道') OR json_extract(i.detail_json, '$.group') IS NULL)"
        else:
            where += " AND json_extract(i.detail_json, '$.group') = ?"
            params.append(group)
    return where, params


def count_feed_by_platform(conn, platform, *, source=None, group=None, category=None,
                           public_only=False, manual_owner_user_id=None):
    """Return the full total for the same filter contract as query_feed_by_platform."""
    where, params = _feed_by_platform_where(
        platform,
        source=source,
        group=group,
        category=category,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    row = conn.execute(f"SELECT COUNT(*) AS cnt FROM items i {where}", params).fetchone()
    return int(row['cnt'] if row else 0)


def query_feed_by_platform(conn, platform, offset=0, limit=50, user_id=None,
                           source=None, group=None, category=None,
                           public_only=False,
                           manual_owner_user_id=None):
    """Query items for a specific platform with pagination.
    当 source 传入时，额外按 source 过滤（用于频道页 pill 切换：
    平台内某个 source 被其他 source 挤出前 50 时依然能拉到数据）。
    BF-0419-10: 当 group 传入时，按 detail_json.group 过滤(公众号订阅的分组维度)。
    v16.0: 当 category 传入时，按 ai_categories JSON array 任意元素过滤
    （L1 维度 section 的 pill 切换；GitHub/Reddit/RSS/HN/WayToAGI/Manual）。
    BF-0512-2+4: ai_categories IS NOT NULL 改为条件式 — 仅 L1 维度切换 require
    （source/group 维度 pill 切换不应过滤 NULL，B 站 watch_later 1024 条全 NULL
     历史数据 + GitHub 33% NULL 历史数据需展示，与顶层 query_feed_platforms 一致）。
    """
    cols = """i.id, i.platform, i.source, i.title, i.cover_url, i.ai_summary,
              i.ai_keywords, i.ai_category, i.ai_categories, i.relevance_score,
              i.fetched_at, i.author_name, i.metrics_json,
              s.clicked_at, s.starred_at"""
    join_cond, join_params = _item_status_join(conn, user_id)
    where, where_params = _feed_by_platform_where(
        platform,
        source=source,
        group=group,
        category=category,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    params = list(join_params) + where_params
    params += [limit, offset]
    sql = f"""
        SELECT {cols}
        FROM items i
        {join_cond}
        {where}
        ORDER BY i.fetched_at DESC, i.relevance_score DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, params).fetchall()
    return [_normalize_item_category(dict(r)) for r in rows]




def get_category_counts(conn, platform, *, user_id=None, public_only=False,
                        manual_owner_user_id=None, days=None):
    """v16.0: 聚合某 platform 下近 N 天每个 L1 分类的 item 数量。

    用于 routes/feed.py 频道页 L1 维度 section（GitHub/Reddit/RSS/HN/WayToAGI/Manual）
    的 pill 动态过滤——只展示当前有数据的 L1，按数据量降序。

    返回: dict[str, int] = {l1_id: count}, e.g. {'products': 12, 'coding': 8}
    BF-0512-6: 同时返回 '__uncategorized__' 键统计 NULL ai_categories 的 item 数,
    供前端「未分类 N」pill 显示。这部分是 v4.0+ 之前 / enrich 失败的历史 item,
    在「全部」pill 里可见但不归属任何 L1。

    BF-0512-6 修订：days 参数保留但默认不再限制窗口（days=None），让 L1 pill cnt
    跟「全部」pill（query_feed_platforms 不限窗口）对账。原 days=7 在 7d 窗口内
    NULL=0 时让「未分类」pill 永远不显示，违反用户视觉预期（「全部」349 vs L1 加和
    很少时用户期望「未分类」pill 解释差距）。
    """
    # 共用的可见性 / display 过滤
    base_where, base_params = ["i.platform = ?"], [platform]
    _add_manual_visibility(
        base_where, base_params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(base_where, base_params)
    base_where.append("i.visible = 1")
    # BF-0512-6: days=None 不加时间窗（默认）；days=N 仅在显式指定时启用
    if days is not None:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        base_where.append("i.fetched_at >= ?")
        base_params.append(cutoff)

    # 1) 有 ai_categories 的 L1 计数（原逻辑）
    cat_where = list(base_where) + ["i.ai_categories IS NOT NULL"]
    cat_where_sql = " AND ".join(cat_where)
    cat_sql = f"""
        SELECT je.value AS category, COUNT(DISTINCT i.id) AS cnt
        FROM items i, json_each(i.ai_categories) je
        WHERE {cat_where_sql}
        GROUP BY je.value
        ORDER BY cnt DESC
    """
    counts = {r['category']: r['cnt'] for r in conn.execute(cat_sql, base_params).fetchall() if r['category']}

    # 2) BF-0512-6: NULL ai_categories 的 item 数 → 「未分类」pill
    null_where = list(base_where) + ["i.ai_categories IS NULL"]
    null_where_sql = " AND ".join(null_where)
    null_sql = f"SELECT COUNT(*) AS cnt FROM items i WHERE {null_where_sql}"
    null_cnt = conn.execute(null_sql, base_params).fetchone()['cnt']
    if null_cnt > 0:
        counts[UNCATEGORIZED_SENTINEL] = null_cnt
    return counts


def get_stats(conn, user_id=None, public_only=False, manual_owner_user_id=None):
    """Get unread counts per platform. user_id scopes item_status to that user."""
    join_cond, params = _item_status_join(conn, user_id)
    where = []
    _add_manual_visibility(
        where, params, public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    _add_display_visibility(where, params)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT i.platform, COUNT(*) as total,
               SUM(CASE WHEN s.clicked_at IS NULL AND s.hidden_at IS NULL THEN 1 ELSE 0 END) as unread
        FROM items i
        {join_cond}
        {where_sql}
        GROUP BY i.platform
    """
    rows = conn.execute(sql, params).fetchall()
    return {r['platform']: {'total': r['total'], 'unread': r['unread']} for r in rows}


def get_last_fetch(conn):
    """Get the most recent fetch run."""
    row = conn.execute(
        "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _json_or_default(raw, default):
    if raw is None or raw == '':
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value):
    if not value:
        return None
    text = str(value).replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace(' ', 'T'))
        except ValueError:
            return None


def _elapsed_seconds(started_at, finished_at):
    start = _parse_datetime(started_at)
    end = _parse_datetime(finished_at)
    if not start or not end:
        return None
    if (start.tzinfo is None) != (end.tzinfo is None):
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)
    return max(0.0, round((end - start).total_seconds(), 2))


def _run_has_item_records(conn, run_id):
    row = conn.execute(
        "SELECT 1 FROM fetch_run_items WHERE run_id = ? LIMIT 1",
        (run_id,),
    ).fetchone()
    return row is not None


def _new_run_items_cte(conn, run_id):
    if _run_has_item_records(conn, run_id):
        return (
            """SELECT i.*
                 FROM fetch_run_items fri
                 JOIN items i ON i.id = fri.item_id
                WHERE fri.run_id = ?
                  AND fri.was_inserted = 1""",
            [run_id],
            'fetch_run_items',
        )
    return (
        """SELECT i.*
             FROM items i
             JOIN fetch_runs r ON r.id = ?
            WHERE i.fetch_run_id = r.id
              AND datetime(i.created_at) >= datetime(r.started_at)
              AND datetime(i.created_at) <= datetime(COALESCE(r.finished_at, 'now'))""",
        [run_id],
        'created_at_fallback',
    )


def _pill_from_item(row):
    cats = _json_or_default(row.get('ai_categories'), None)
    if isinstance(cats, list) and cats:
        return str(cats[0] or '_uncategorized')
    return canonicalize_category(row.get('ai_category')) or '_uncategorized'


def _extract_fetch_errors(stats):
    errors = []
    if not isinstance(stats, dict):
        return errors
    for key, value in stats.items():
        if key.startswith('_'):
            continue
        if isinstance(value, dict):
            for err in value.get('errors') or []:
                errors.append({'scope': key, 'message': str(err)})
        elif isinstance(value, list):
            for err in value:
                errors.append({'scope': key, 'message': str(err)})
    return errors[:20]


def record_embedding_usage(log):
    """Persist one embedding provider call for cost/debug audit."""
    if not isinstance(log, dict):
        return None
    payload = dict(log)
    payload.setdefault('created_at', datetime.now().isoformat())
    item_ids = payload.get('item_ids_json')
    if isinstance(item_ids, (list, tuple)):
        payload['item_ids_json'] = json.dumps(list(item_ids), ensure_ascii=False)
    columns = (
        'created_at',
        'provider',
        'model',
        'mode',
        'source',
        'stage',
        'run_id',
        'caller_file',
        'caller_func',
        'input_count',
        'input_chars',
        'input_bytes',
        'estimated_tokens',
        'token_estimator',
        'output_count',
        'output_dim',
        'status',
        'error',
        'latency_ms',
        'price_yuan_per_1k_tokens',
        'estimated_cost_yuan',
        'item_ids_json',
    )
    values = [payload.get(col) for col in columns]
    conn = get_conn()
    try:
        cur = conn.execute(
            f"""INSERT INTO embedding_usage_logs ({','.join(columns)})
                VALUES ({','.join(['?'] * len(columns))})""",
            values,
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _embedding_usage_where(hours=None, run_id=None):
    clauses = []
    params = []
    if hours is not None:
        try:
            hours_float = float(hours)
        except (TypeError, ValueError):
            hours_float = 24.0
        if hours_float > 0:
            since = (datetime.now() - timedelta(hours=hours_float)).isoformat()
            clauses.append("datetime(created_at) >= datetime(?)")
            params.append(since)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(int(run_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_embedding_usage_audit(conn, *, hours=24, run_id=None, limit=100):
    """Return aggregate + recent call rows for embedding usage monitoring."""
    where, params = _embedding_usage_where(hours=hours, run_id=run_id)
    summary = conn.execute(
        f"""SELECT COUNT(*) AS total_calls,
                   COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                   COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failed_calls,
                   COALESCE(SUM(input_count), 0) AS input_count,
                   COALESCE(SUM(input_chars), 0) AS input_chars,
                   COALESCE(SUM(input_bytes), 0) AS input_bytes,
                   COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens_attempted,
                   COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_tokens ELSE 0 END), 0) AS estimated_tokens_success,
                   COALESCE(SUM(output_count), 0) AS output_count,
                   COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_cost_yuan ELSE 0 END), 0.0) AS estimated_cost_yuan_success,
                   COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan_all
              FROM embedding_usage_logs
              {where}""",
        params,
    ).fetchone()
    by_source = conn.execute(
        f"""SELECT COALESCE(source, 'unknown') AS source,
                   COALESCE(stage, '') AS stage,
                   provider,
                   model,
                   status,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_count), 0) AS input_count,
                   COALESCE(SUM(input_chars), 0) AS input_chars,
                   COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                   COALESCE(SUM(output_count), 0) AS output_count,
                   COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
              FROM embedding_usage_logs
              {where}
             GROUP BY source, stage, provider, model, status
             ORDER BY estimated_tokens DESC, calls DESC
             LIMIT 50""",
        params,
    ).fetchall()
    by_run = conn.execute(
        f"""SELECT run_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                   COALESCE(SUM(input_count), 0) AS input_count,
                   COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                   COALESCE(SUM(output_count), 0) AS output_count,
                   COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
              FROM embedding_usage_logs
              {where}
             GROUP BY run_id
             ORDER BY MAX(datetime(created_at)) DESC
             LIMIT 50""",
        params,
    ).fetchall()
    bounded_limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        f"""SELECT *
              FROM embedding_usage_logs
              {where}
             ORDER BY datetime(created_at) DESC, id DESC
             LIMIT ?""",
        params + [bounded_limit],
    ).fetchall()
    return {
        'hours': hours,
        'run_id': run_id,
        'summary': dict(summary) if summary else {},
        'by_source': [dict(r) for r in by_source],
        'by_run': [dict(r) for r in by_run],
        'logs': [dict(r) for r in rows],
        'limit': bounded_limit,
    }


def build_fetch_run_audit_summary(conn, run_id, raw_stats=None, finished_at=None):
    """Build the v15.2 fetch-run audit summary without storing item lists."""
    run = conn.execute("SELECT * FROM fetch_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return {}
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    items_sql, item_params, source = _new_run_items_cte(conn, run_id)

    rows = conn.execute(
        f"""SELECT platform, source, COUNT(*) AS count
              FROM ({items_sql}) ni
             GROUP BY platform, source
             ORDER BY count DESC, platform, source""",
        item_params,
    ).fetchall()
    platform_source = [dict(r) for r in rows]

    platform_counts = {}
    for row in platform_source:
        platform = row.get('platform') or 'unknown'
        platform_counts[platform] = platform_counts.get(platform, 0) + int(row.get('count') or 0)

    item_rows = conn.execute(
        f"""SELECT id, ai_summary, ai_error_count, ai_last_error,
                   ai_categories, ai_category, cluster_id
              FROM ({items_sql}) ni""",
        item_params,
    ).fetchall()
    pill_counts = {}
    summarized = 0
    ai_failed = 0
    clustered_items = 0
    touched_clusters = set()
    for raw_row in item_rows:
        row = dict(raw_row)
        pill = _pill_from_item(row)
        pill_counts[pill] = pill_counts.get(pill, 0) + 1
        if row.get('ai_summary'):
            summarized += 1
        if (row.get('ai_error_count') or 0) > 0 or row.get('ai_last_error'):
            ai_failed += 1
        if row.get('cluster_id') is not None:
            clustered_items += 1
            touched_clusters.add(row.get('cluster_id'))

    published_clusters = conn.execute(
        "SELECT COUNT(*) AS count FROM clusters WHERE published_run_id = ?",
        (run_id,),
    ).fetchone()

    ended_at = finished_at or run['finished_at']
    stage_durations = raw_stats.get('_stage_durations_sec') or raw_stats.get('stage_durations_sec') or {}
    result_status = raw_stats.get('_result_status')
    total_new = len(item_rows)
    return {
        'version': 'v15.2',
        'run_id': run_id,
        'source': source,
        'duration_sec': _elapsed_seconds(run['started_at'], ended_at),
        'stage_durations_sec': stage_durations if isinstance(stage_durations, dict) else {},
        'result_status': result_status,
        'new_items_count': total_new,
        'platform_counts': [
            {'platform': key, 'count': value}
            for key, value in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        'platform_source_counts': platform_source,
        'pill_counts': [
            {'pill': key, 'count': value}
            for key, value in sorted(pill_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        'ai_summary': {
            'summarized': summarized,
            'failed': ai_failed,
            'pending': max(0, total_new - summarized - ai_failed),
        },
        'event_cluster': {
            'clustered_items': clustered_items,
            'touched_clusters': len(touched_clusters),
            'published_clusters': int(published_clusters['count'] if published_clusters else 0),
        },
        'errors': _extract_fetch_errors(raw_stats),
    }


def _fetch_run_to_audit_dict(conn, row):
    data = dict(row)
    stats = _json_or_default(data.get('stats_json'), {})
    audit = stats.get('_audit') if isinstance(stats, dict) else None
    if not isinstance(audit, dict):
        audit = build_fetch_run_audit_summary(conn, data['id'], stats)
    data['stats'] = stats if isinstance(stats, dict) else {}
    data['audit'] = audit
    data['duration_sec'] = audit.get('duration_sec') or _elapsed_seconds(data.get('started_at'), data.get('finished_at'))
    data['total_new_items'] = audit.get('new_items_count', 0)
    return data


def list_fetch_run_audits(conn, limit=50, offset=0):
    rows = conn.execute(
        """SELECT * FROM fetch_runs
           ORDER BY started_at DESC, id DESC
           LIMIT ? OFFSET ?""",
        (max(1, min(int(limit or 50), 100)), max(0, int(offset or 0))),
    ).fetchall()
    return [_fetch_run_to_audit_dict(conn, row) for row in rows]


def get_fetch_run_audit(conn, run_id):
    row = conn.execute("SELECT * FROM fetch_runs WHERE id = ?", (run_id,)).fetchone()
    return _fetch_run_to_audit_dict(conn, row) if row else None


def query_fetch_run_audit_items(conn, run_id, platform=None, source=None, limit=50, offset=0):
    items_sql, item_params, source_kind = _new_run_items_cte(conn, run_id)
    where = []
    params = list(item_params)
    if platform:
        where.append("ni.platform = ?")
        params.append(platform)
    if source:
        where.append("ni.source = ?")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    bounded_limit = max(1, min(int(limit or 50), 100))
    bounded_offset = max(0, int(offset or 0))
    total = conn.execute(
        f"SELECT COUNT(*) AS count FROM ({items_sql}) ni {where_sql}",
        params,
    ).fetchone()
    rows = conn.execute(
        f"""SELECT ni.id, ni.title, ni.platform, ni.source, ni.url,
                   ni.ai_summary, ni.ai_error_count, ni.ai_last_error,
                   ni.ai_category, ni.ai_categories, ni.cluster_id,
                   ni.created_at, ni.fetched_at
              FROM ({items_sql}) ni
              {where_sql}
             ORDER BY datetime(ni.created_at) DESC, ni.id DESC
             LIMIT ? OFFSET ?""",
        params + [bounded_limit, bounded_offset],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['pill'] = _pill_from_item(item)
        item['ai_status'] = 'failed' if (item.get('ai_error_count') or 0) > 0 or item.get('ai_last_error') else (
            'summarized' if item.get('ai_summary') else 'pending'
        )
        item['cluster_status'] = 'clustered' if item.get('cluster_id') is not None else 'pending'
        items.append(item)
    return {
        'items': items,
        'total': int(total['count'] if total else 0),
        'source': source_kind,
        'limit': bounded_limit,
        'offset': bounded_offset,
    }


def start_fetch_run(conn):
    """Record a new fetch run starting."""
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO fetch_runs (started_at) VALUES (?)", (now,)
    )
    conn.commit()
    return cur.lastrowid


def finish_fetch_run(conn, run_id, stats, error=None):
    """Mark a fetch run as complete."""
    now = datetime.now().isoformat()
    stats_payload = dict(stats or {}) if isinstance(stats, dict) else {'value': stats}
    stats_payload['_audit'] = build_fetch_run_audit_summary(conn, run_id, stats_payload, finished_at=now)
    conn.execute("""
        UPDATE fetch_runs SET finished_at=?, status=?, stats_json=?, error_msg=?
        WHERE id=?
    """, (now, 'error' if error else 'done', json.dumps(stats_payload, ensure_ascii=False), error, run_id))
    conn.commit()


def add_feedback(conn, item_id, fb_type, topic=None, text=None):
    """Add user feedback for an item."""
    conn.execute("""
        INSERT INTO feedback (item_id, type, topic, text) VALUES (?, ?, ?, ?)
    """, (item_id, fb_type, topic, text))
    conn.commit()


def get_feedback_scores(conn):
    """Get aggregated feedback scores per author and source.
    Returns dict: { author_scores: {name: score}, source_scores: {source: score}, item_feedback: {item_id: [types]} }
    """
    # Per-author: positive=+1, low_quality=-1
    author_sql = """
        SELECT i.author_name, f.type, COUNT(*) as cnt
        FROM feedback f JOIN items i ON f.item_id = i.id
        WHERE f.type IN ('positive', 'low_quality') AND i.author_name != ''
        GROUP BY i.author_name, f.type
    """
    author_scores = {}
    for r in conn.execute(author_sql).fetchall():
        name = r['author_name']
        if name not in author_scores:
            author_scores[name] = 0
        author_scores[name] += r['cnt'] if r['type'] == 'positive' else -r['cnt']

    # Per-item feedback types
    item_sql = "SELECT item_id, type FROM feedback"
    item_fb = {}
    for r in conn.execute(item_sql).fetchall():
        if r['item_id'] not in item_fb:
            item_fb[r['item_id']] = []
        item_fb[r['item_id']].append(r['type'])

    # Per-item text feedback
    text_sql = "SELECT item_id, text, created_at FROM feedback WHERE type='text' AND text IS NOT NULL AND text != '' ORDER BY created_at DESC"
    text_fb = {}
    for r in conn.execute(text_sql).fetchall():
        if r['item_id'] not in text_fb:
            text_fb[r['item_id']] = []
        text_fb[r['item_id']].append({'text': r['text'], 'created_at': r['created_at']})

    return {'author_scores': author_scores, 'item_feedback': item_fb, 'text_feedback': text_fb}


def record_keywords(conn, keywords, platform):
    """Record which search keywords were used."""
    now = datetime.now().isoformat()
    for kw in keywords:
        conn.execute("""
            INSERT INTO search_keywords (keyword, platform, last_used_at) VALUES (?, ?, ?)
            ON CONFLICT(keyword, platform) DO UPDATE SET last_used_at = ?
        """, (kw, platform, now, now))
    conn.commit()


def update_ai_summary(conn, item_id, summary, key_points, relevance=None):
    """Write AI-generated summary, key points, and relevance score to an item."""
    key_points_json = json.dumps(key_points, ensure_ascii=False) if key_points else None
    if relevance is not None:
        conn.execute("""
            UPDATE items
            SET ai_summary = ?, ai_key_points = ?, ai_relevance = ?,
                ai_error_count = 0, ai_last_error = NULL,
                ai_last_error_at = NULL, ai_retry_after = NULL
            WHERE id = ?
        """, (summary, key_points_json, relevance, item_id))
    else:
        conn.execute("""
            UPDATE items
            SET ai_summary = ?, ai_key_points = ?,
                ai_error_count = 0, ai_last_error = NULL,
                ai_last_error_at = NULL, ai_retry_after = NULL
            WHERE id = ?
        """, (summary, key_points_json, item_id))
    conn.commit()


def _sqlite_time_after(seconds):
    return (datetime.utcnow() + timedelta(seconds=int(seconds))).strftime("%Y-%m-%d %H:%M:%S")


def record_ai_failure(conn, item_id, error, retry_after=None, increment=True):
    """Record an item-level AI failure without marking provider-level cooldown."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(retry_after, (int, float)):
        retry_after_value = _sqlite_time_after(retry_after)
    else:
        retry_after_value = retry_after

    count_expr = "COALESCE(ai_error_count, 0) + 1" if increment else "COALESCE(ai_error_count, 0)"
    conn.execute(f"""
        UPDATE items
        SET ai_error_count = {count_expr},
            ai_last_error = ?,
            ai_last_error_at = ?,
            ai_retry_after = ?
        WHERE id = ?
    """, (str(error)[:500], now, retry_after_value, item_id))
    conn.commit()


def clear_ai_failure(conn, item_id):
    """Clear item-level AI retry/backoff state after a successful enrichment."""
    conn.execute("""
        UPDATE items
        SET ai_error_count = 0,
            ai_last_error = NULL,
            ai_last_error_at = NULL,
            ai_retry_after = NULL
        WHERE id = ?
    """, (item_id,))
    conn.commit()


# ══════════════════════════════════════════════════
# v5.0: Briefing CRUD
# ══════════════════════════════════════════════════

def upsert_briefing(conn, briefing_id, date, insights, suggestions, input_count, model):
    """Insert or update a briefing (overwrite if same date)."""
    insights_json = json.dumps(insights, ensure_ascii=False) if insights else '[]'
    suggestions_json = json.dumps(suggestions, ensure_ascii=False) if suggestions else '[]'
    now = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO briefings (id, date, insights, suggestions, input_count, model, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            insights = excluded.insights,
            suggestions = excluded.suggestions,
            input_count = excluded.input_count,
            model = excluded.model,
            created_at = excluded.created_at
    """, (briefing_id, date, insights_json, suggestions_json, input_count, model, now))
    conn.commit()


def get_briefing(conn, date=None):
    """Get briefing by date (defaults to today). Returns dict or None."""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    row = conn.execute(
        "SELECT * FROM briefings WHERE date = ? ORDER BY created_at DESC LIMIT 1",
        (date,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    for col in ('insights', 'suggestions'):
        val = result.get(col)
        if val and isinstance(val, str):
            try:
                result[col] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return result


def list_briefing_dates(conn, limit=30):
    """List available briefing dates (most recent first)."""
    rows = conn.execute(
        "SELECT DISTINCT date FROM briefings ORDER BY date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [r['date'] for r in rows]


# ══════════════════════════════════════════════════
# v8.0: Actions CRUD (new schema with UUID PK)
# ══════════════════════════════════════════════════

import uuid as _uuid

def create_action(conn, *, source_type, title, action_type, prompt,
                  source_item_ids=None, reason=None, priority='medium',
                  related_project=None, status='pending',
                  direction='_uncategorized', direction_label='待归类',
                  user_id=None, steps=None):
    """Create a new action with v8.0 schema. Returns the new action UUID.

    v21.0: `steps`(list|None)是人看的结构化行动点,与自包含 `prompt`(机器执行)分离;
    存 JSON 文本,read model 优先读它、无则回退拆 prompt。
    """
    action_id = str(_uuid.uuid4())
    source_ids_json = json.dumps(source_item_ids or [], ensure_ascii=False)
    steps_json = json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) and steps else None
    conn.execute("""
        INSERT INTO actions (id, user_id, source_type, source_item_ids,
            original_title, original_prompt, original_reason, original_priority,
            title, action_type, related_project, prompt, steps, reason, priority, status,
            direction, direction_label)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (action_id, user_id, source_type, source_ids_json,
          title, prompt, reason, priority,
          title, action_type, related_project, prompt, steps_json, reason, priority, status,
          direction, direction_label))
    conn.commit()
    # Log creation event
    _log_action_event(conn, action_id, 'created', {
        'source_item_ids': source_item_ids or [],
        'source_type': source_type
    })
    return action_id


def _log_action_event(conn, action_id, event_type, detail=None):
    """Write an action_logs entry."""
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    conn.execute("""
        INSERT INTO action_logs (action_id, event_type, detail_json) VALUES (?, ?, ?)
    """, (action_id, event_type, detail_json))
    conn.commit()


_ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS = (
    'created_at',
    'confirmed_at',
    'executed_at',
    'completed_at',
    'dismissed_at',
    'dispatched_at',
    'project_context_updated_at',
)


def _action_source_updated_at(action):
    if not action:
        return None
    values = [
        action.get(field)
        for field in _ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS
        if action.get(field)
    ]
    if not values:
        return None
    return max(values, key=sort_key)


def _action_detail_read_model_fresh(row):
    source_updated_at = _action_source_updated_at(row)
    if not source_updated_at:
        return True
    cached_source_updated_at = row.get('source_updated_at')
    if not cached_source_updated_at:
        return False
    return sort_key(cached_source_updated_at) >= sort_key(source_updated_at)


def get_action_detail_read_model(conn, action_id, viewer_scope='owner', owner_user_id=None):
    """Return a prebuilt action detail payload, or None if it is absent/stale."""
    where = "rm.action_id = ? AND viewer_scope = ? AND payload_version = ?"
    params = [action_id, viewer_scope, action_detail_read_model.READ_MODEL_VERSION]
    if owner_user_id and viewer_scope != 'admin':
        where += " AND owner_user_id = ?"
        params.append(owner_user_id)
    row = conn.execute(
        f"""SELECT rm.payload_json, rm.source_updated_at,
                   a.created_at, a.confirmed_at, a.executed_at,
                   a.completed_at, a.dismissed_at, a.dispatched_at,
                   a.project_context_updated_at
              FROM action_detail_read_models rm
              JOIN actions a ON a.id = rm.action_id
             WHERE {where}""",
        params,
    ).fetchone()
    if not row:
        return None
    if not _action_detail_read_model_fresh(dict(row)):
        return None
    try:
        return json.loads(row['payload_json'])
    except (json.JSONDecodeError, TypeError):
        return None


def get_action_detail_read_models(conn, action_ids, viewer_scope='owner', owner_user_id=None):
    """Return prebuilt action detail payloads keyed by action id."""
    ids = list(dict.fromkeys(str(action_id) for action_id in action_ids if action_id))
    if not ids:
        return {}
    placeholders = ", ".join(["?"] * len(ids))
    where = (
        f"action_id IN ({placeholders}) "
        "AND viewer_scope = ? AND payload_version = ?"
    )
    params = [*ids, viewer_scope, action_detail_read_model.READ_MODEL_VERSION]
    if owner_user_id and viewer_scope != 'admin':
        where += " AND owner_user_id = ?"
        params.append(owner_user_id)
    rows = conn.execute(
        f"""SELECT rm.action_id, rm.payload_json, rm.source_updated_at,
                   a.created_at, a.confirmed_at, a.executed_at,
                   a.completed_at, a.dismissed_at, a.dispatched_at,
                   a.project_context_updated_at
              FROM action_detail_read_models rm
              JOIN actions a ON a.id = rm.action_id
             WHERE {where.replace('action_id', 'rm.action_id', 1)}""",
        params,
    ).fetchall()
    out = {}
    for row in rows:
        if not _action_detail_read_model_fresh(dict(row)):
            continue
        try:
            payload = json.loads(row['payload_json'])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            out[str(row['action_id'])] = payload
    return out


def upsert_action_detail_read_model(
    conn,
    *,
    action_id,
    viewer_scope='owner',
    owner_user_id=None,
    payload,
    source_item_ids=None,
    source_updated_at=None,
):
    """Persist the display-ready payload used by /api/actions/{id}."""
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        default=action_detail_read_model.json_default,
    )
    source_json = json.dumps(source_item_ids or [], ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO action_detail_read_models
          (action_id, viewer_scope, owner_user_id, payload_json, source_item_ids,
           payload_version, built_at, source_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
        ON CONFLICT(action_id, viewer_scope) DO UPDATE SET
          owner_user_id = excluded.owner_user_id,
          payload_json = excluded.payload_json,
          source_item_ids = excluded.source_item_ids,
          payload_version = excluded.payload_version,
          built_at = excluded.built_at,
          source_updated_at = excluded.source_updated_at
        """,
        (
            action_id,
            viewer_scope,
            owner_user_id,
            payload_json,
            source_json,
            action_detail_read_model.READ_MODEL_VERSION,
            source_updated_at,
        ),
    )
    conn.commit()


def delete_action_detail_read_model(conn, action_id, viewer_scope=None):
    """Delete cached action detail payloads for one action."""
    if viewer_scope:
        conn.execute(
            "DELETE FROM action_detail_read_models WHERE action_id = ? AND viewer_scope = ?",
            (action_id, viewer_scope),
        )
    else:
        conn.execute("DELETE FROM action_detail_read_models WHERE action_id = ?", (action_id,))
    conn.commit()


def get_action_source_items(conn, source_ids, *, request_user_id=None, can_view_all=False):
    """Resolve source item rows with the same privacy rule as action detail."""
    out = []
    for sid in action_detail_read_model.parse_source_item_ids(source_ids):
        row = conn.execute(
            "SELECT id, user_id, platform, title, ai_summary, url, detail_json FROM items WHERE id = ?",
            (sid,),
        ).fetchone()
        if not row:
            continue
        item = dict(row)
        if (
            item.get('platform') == 'manual'
            and not can_view_all
            and item.get('user_id') != request_user_id
        ):
            continue
        detail_json = item.get('detail_json')
        if detail_json:
            try:
                detail = json.loads(detail_json) if isinstance(detail_json, str) else detail_json
                item['referenced_urls'] = detail.get('referenced_urls', []) if isinstance(detail, dict) else []
            except (json.JSONDecodeError, TypeError):
                item['referenced_urls'] = []
        else:
            item['referenced_urls'] = []
        item.pop('detail_json', None)
        item.pop('user_id', None)
        out.append(item)
    return out


def build_action_detail_read_model(
    conn,
    action_id,
    *,
    request_user_id=None,
    can_view_all=False,
    owner_user_id=None,
    execution_status=None,
    persist=True,
):
    """Build and optionally persist the complete action detail read payload."""
    action = get_action(conn, action_id, user_id=None if can_view_all else owner_user_id)
    if not action:
        return None
    source_ids = action_detail_read_model.parse_source_item_ids(action.get('source_item_ids'))
    source_items = get_action_source_items(
        conn,
        source_ids,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
    )
    payload = action_detail_read_model.build_action_detail_payload(
        action,
        source_items=source_items,
        execution_status=execution_status,
    )
    if persist:
        upsert_action_detail_read_model(
            conn,
            action_id=action_id,
            viewer_scope=action_detail_read_model.viewer_scope_for(can_view_all=can_view_all),
            owner_user_id=action.get('user_id'),
            payload=payload,
            source_item_ids=source_ids,
            source_updated_at=_action_source_updated_at(action),
        )
    return payload


def get_actions(conn, status=None, priority=None, action_type=None, direction=None, user_id=None):
    """Query actions with optional filters. Returns list of dicts."""
    where, params = [], []
    if user_id:
        where.append("user_id = ?"); params.append(user_id)
    if status == 'in_progress':
        where.append("status IN (?, ?, ?)")
        params.extend(['confirmed', 'executing', 'dispatched'])
    elif status:
        where.append("status = ?"); params.append(status)
    if priority:
        where.append("priority = ?"); params.append(priority)
    if action_type:
        where.append("action_type = ?"); params.append(action_type)
    if direction:
        where.append("direction = ?"); params.append(direction)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(f"""
        SELECT * FROM actions {where_sql}
        ORDER BY
            CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                        WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                        WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                        WHEN 'dismissed' THEN 4 ELSE 5 END,
            CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                          WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
            created_at DESC
    """, params).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields
        for field in ('source_item_ids',):
            val = d.get(field)
            if val and isinstance(val, str):
                try:
                    d[field] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
        results.append(d)
    return results


def get_action(conn, action_id, user_id=None):
    """Get single action by UUID. Returns dict or None."""
    if user_id:
        row = conn.execute(
            "SELECT * FROM actions WHERE id = ? AND user_id = ?",
            (action_id, user_id),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for field in ('source_item_ids',):
        val = d.get(field)
        if val and isinstance(val, str):
            try:
                d[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def update_action(conn, action_id, owner_user_id=None, **fields):
    """Update an action's fields. Returns True if found."""
    allowed = {'title', 'prompt', 'reason', 'priority', 'status', 'action_type',
               'related_project', 'source_item_ids', 'direction', 'direction_label',
               'execution_tool', 'execution_result',
               'execution_exit_code', 'execution_model', 'execution_duration_seconds',
               'session_id', 'project_context', 'project_context_updated_at',
               'confirmed_at', 'executed_at', 'completed_at', 'dismissed_at',
               'discord_thread_id', 'discord_thread_url', 'dispatched_at'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    vals = list(updates.values()) + [action_id]
    where = "id = ?"
    if owner_user_id:
        where += " AND user_id = ?"
        vals.append(owner_user_id)
    cur = conn.execute(f"UPDATE actions SET {set_clause} WHERE {where}", vals)
    conn.commit()
    return cur.rowcount > 0


def delete_action(conn, action_id, owner_user_id=None):
    """Delete an action and its logs/feedback."""
    if owner_user_id and not get_action(conn, action_id, user_id=owner_user_id):
        return False
    conn.execute("DELETE FROM action_feedback WHERE action_id = ?", (action_id,))
    conn.execute("DELETE FROM action_logs WHERE action_id = ?", (action_id,))
    if owner_user_id:
        cur = conn.execute(
            "DELETE FROM actions WHERE id = ? AND user_id = ?",
            (action_id, owner_user_id),
        )
    else:
        cur = conn.execute("DELETE FROM actions WHERE id = ?", (action_id,))
    conn.commit()
    return cur.rowcount > 0


def get_action_counts(conn, user_id=None):
    """Get action counts by status."""
    if user_id:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM actions WHERE user_id = ? GROUP BY status
        """, (user_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM actions GROUP BY status
        """).fetchall()
    counts = {r['status']: r['cnt'] for r in rows}
    return counts


def add_action_feedback(conn, action_id, phase, rating, comment=None):
    """Add explicit user feedback for an action."""
    conn.execute("""
        INSERT INTO action_feedback (action_id, phase, rating, comment) VALUES (?, ?, ?, ?)
    """, (action_id, phase, rating, comment))
    conn.commit()
    _log_action_event(conn, action_id, 'feedback', {
        'phase': phase, 'rating': rating, 'comment': comment
    })


def get_recent_action_feedback(conn, limit=20):
    """Get recent action feedback for LLM context injection."""
    rows = conn.execute("""
        SELECT af.*, a.title, a.original_title, a.reason
        FROM action_feedback af
        JOIN actions a ON af.action_id = a.id
        ORDER BY af.created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_recent_dismissed_actions(conn, limit=10):
    """Get recently dismissed actions for LLM learning."""
    rows = conn.execute("""
        SELECT a.title, a.reason, a.priority, a.action_type,
               al.detail_json
        FROM actions a
        LEFT JOIN action_logs al ON a.id = al.action_id AND al.event_type = 'dismissed'
        WHERE a.status = 'dismissed'
        ORDER BY a.dismissed_at DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_recent_edited_actions(conn, limit=10):
    """Get recently edited actions (original vs current diff) for LLM learning."""
    rows = conn.execute("""
        SELECT id, original_title, title, original_prompt, prompt,
               original_reason, reason, original_priority, priority
        FROM actions
        WHERE original_title != title OR original_prompt != prompt
              OR original_reason != reason OR original_priority != priority
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════
# v6.0: Interests CRUD
# ══════════════════════════════════════════════════

def create_interest(conn, name, description=None, keywords=None, sort='relevance',
                    item_limit=30, scope='all', user_id=None):
    """创建兴趣配置，返回新 interest id。"""
    keywords_json = json.dumps(keywords or [], ensure_ascii=False)
    now = datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO interests (user_id, name, description, keywords, sort, item_limit, scope, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, name, description, keywords_json, sort, item_limit, scope, now))
    conn.commit()
    return cur.lastrowid


def _parse_interest_json_fields(d):
    """Parse JSON fields (keywords, suggestion) in an interest dict."""
    for field in ('keywords', 'suggestion'):
        val = d.get(field)
        if val and isinstance(val, str):
            try:
                d[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[field] = [] if field == 'keywords' else None
    return d


def list_interests(conn, user_id=None):
    """列出所有兴趣配置。"""
    if user_id:
        rows = conn.execute(
            "SELECT * FROM interests WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM interests ORDER BY created_at DESC").fetchall()
    results = []
    for r in rows:
        d = _parse_interest_json_fields(dict(r))
        results.append(d)
    return results


def get_interest(conn, interest_id, user_id=None):
    """获取单个兴趣配置，返回 dict 或 None。"""
    if user_id:
        row = conn.execute(
            "SELECT * FROM interests WHERE id = ? AND user_id = ?",
            (interest_id, user_id),
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM interests WHERE id = ?", (interest_id,)).fetchone()
    if not row:
        return None
    return _parse_interest_json_fields(dict(row))


def update_interest(conn, interest_id, owner_user_id=None, **fields):
    """更新兴趣配置字段。支持: name, description, keywords, sort, item_limit, scope, enabled, scan_status, last_scan_at。"""
    allowed = {'name', 'description', 'keywords', 'sort', 'item_limit', 'scope',
               'enabled', 'scan_status', 'last_scan_at', 'suggestion'}
    updates = {}
    for k, v in fields.items():
        if k in allowed and v is not None:
            if k == 'keywords' and isinstance(v, (list, tuple)):
                updates[k] = json.dumps(v, ensure_ascii=False)
            else:
                updates[k] = v
    if not updates:
        return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    vals = list(updates.values()) + [interest_id]
    where = "id = ?"
    if owner_user_id:
        where += " AND user_id = ?"
        vals.append(owner_user_id)
    cur = conn.execute(f"UPDATE interests SET {set_clause} WHERE {where}", vals)
    conn.commit()
    return cur.rowcount > 0


def delete_interest(conn, interest_id, owner_user_id=None):
    """删除兴趣配置并级联删除匹配结果。"""
    if owner_user_id and not get_interest(conn, interest_id, user_id=owner_user_id):
        return False
    conn.execute("DELETE FROM interest_matches WHERE interest_id = ?", (interest_id,))
    if owner_user_id:
        cur = conn.execute(
            "DELETE FROM interests WHERE id = ? AND user_id = ?",
            (interest_id, owner_user_id),
        )
    else:
        cur = conn.execute("DELETE FROM interests WHERE id = ?", (interest_id,))
    conn.commit()
    return cur.rowcount > 0


def upsert_interest_matches(conn, interest_id, matches):
    """批量插入/更新兴趣匹配结果。matches: [{item_id, relevance_score}, ...]"""
    now = datetime.now().isoformat()
    for m in matches:
        conn.execute("""
            INSERT INTO interest_matches (interest_id, item_id, relevance_score, is_new, matched_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(interest_id, item_id) DO UPDATE SET
                relevance_score = excluded.relevance_score,
                matched_at = excluded.matched_at
        """, (interest_id, m['item_id'], m['relevance_score'], now))
    conn.commit()


def get_interest_matches(conn, interest_id, sort='relevance', limit=30, offset=0):
    """获取兴趣匹配结果，关联 items 详情。"""
    order = "m.relevance_score DESC" if sort == 'relevance' else "i.fetched_at DESC"
    rows = conn.execute(f"""
        SELECT m.interest_id, m.item_id, m.relevance_score, m.is_new, m.matched_at,
               i.platform, i.source, i.title, i.content, i.author_name, i.url,
               i.cover_url, i.ai_summary, i.ai_key_points, i.ai_category,
               i.relevance_score as item_score, i.fetched_at, i.published_at,
               s.clicked_at, s.starred_at
        FROM interest_matches m
        JOIN items i ON m.item_id = i.id
        LEFT JOIN item_status s ON i.id = s.item_id
        WHERE m.interest_id = ?
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """, (interest_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_interest_match_stats(conn, interest_id):
    """获取兴趣匹配统计：总数、新增数。"""
    row = conn.execute("""
        SELECT COUNT(*) as total, SUM(CASE WHEN is_new = 1 THEN 1 ELSE 0 END) as new_count
        FROM interest_matches WHERE interest_id = ?
    """, (interest_id,)).fetchone()
    return {'total': row['total'] or 0, 'new_count': row['new_count'] or 0}


def mark_interest_matches_read(conn, interest_id):
    """标记某个兴趣的所有匹配结果为已读（is_new=0）。"""
    conn.execute("UPDATE interest_matches SET is_new = 0 WHERE interest_id = ?", (interest_id,))
    conn.commit()


def get_all_interest_keywords(conn):
    """获取所有启用的兴趣方向的关键词（用于 AI 洞察排除）。"""
    rows = conn.execute("SELECT keywords FROM interests WHERE enabled = 1").fetchall()
    all_kw = []
    for r in rows:
        val = r['keywords']
        if val and isinstance(val, str):
            try:
                kws = json.loads(val)
                if isinstance(kws, list):
                    all_kw.extend(kws)
            except (json.JSONDecodeError, TypeError):
                pass
    return all_kw


# ── v11.0: User authentication CRUD ─────────────────────────

def migrate_item_status_add_user_id(conn, default_user_id):
    """Migrate item_status to composite PK (user_id, item_id).

    Only runs if item_status doesn't have user_id column yet.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(item_status)").fetchall()]
    if 'user_id' in cols:
        return  # already migrated

    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS item_status_new (
            user_id    TEXT NOT NULL,
            item_id    TEXT NOT NULL,
            read_at    TEXT,
            clicked_at TEXT,
            starred_at TEXT,
            hidden_at  TEXT,
            PRIMARY KEY (user_id, item_id)
        );
        INSERT OR IGNORE INTO item_status_new (user_id, item_id, read_at, clicked_at, starred_at, hidden_at)
            SELECT '{default_user_id}', item_id, read_at, clicked_at, starred_at, hidden_at
            FROM item_status;
        DROP TABLE item_status;
        ALTER TABLE item_status_new RENAME TO item_status;
        CREATE INDEX IF NOT EXISTS idx_item_status_user ON item_status(user_id);
    """)
    print(f"[db] item_status migrated to composite PK with default user_id={default_user_id[:8]}...")


def create_user(conn, user_id, username, email, password_hash, role='user'):
    """Create a new user."""
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, email, password_hash, role)
    )
    conn.commit()
    return get_user(conn, user_id)


def create_user_with_invite(conn, user_id, username, email, password_hash,
                            invite_code, verification_code,
                            verification_code_expires, role='user'):
    """Create a user and consume an invite in one transaction."""
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, email, password_hash, role)
        )
        cur = conn.execute(
            "UPDATE invite_codes SET used_count = used_count + 1 WHERE code = ? AND used_count < max_uses",
            (invite_code,),
        )
        if cur.rowcount <= 0:
            conn.rollback()
            return False
        row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (invite_code,)).fetchone()
        used_by = json.loads(row['used_by']) if row['used_by'] else []
        used_by.append(user_id)
        conn.execute(
            "UPDATE invite_codes SET used_by = ? WHERE code = ?",
            (json.dumps(used_by), invite_code)
        )
        conn.execute(
            """UPDATE users
                  SET verification_code = ?,
                      verification_code_expires = ?
                WHERE id = ?""",
            (verification_code, verification_code_expires, user_id)
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def create_user_open(conn, user_id, username, email, password_hash,
                     verification_code, verification_code_expires, role='user'):
    """P1-4 开放注册:创建用户并写入验证码,不消耗邀请码。"""
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, email, password_hash, role)
        )
        conn.execute(
            """UPDATE users
                  SET verification_code = ?,
                      verification_code_expires = ?
                WHERE id = ?""",
            (verification_code, verification_code_expires, user_id)
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def get_user(conn, user_id):
    """Get user by ID."""
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_login(conn, login):
    """Get user by email or username."""
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? OR username = ?",
        (login, login)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_username(conn, username):
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(conn, email):
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def update_user(conn, user_id, **fields):
    """Update user fields."""
    allowed = {'username', 'email', 'password_hash', 'role', 'discord_bot_token_enc', 'last_login_at',
                'email_verified', 'verification_code', 'verification_code_expires',
                'reset_token', 'reset_token_expires', 'discord_channel_id'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?",
                 (*updates.values(), user_id))
    conn.commit()


def list_users(conn):
    """List all users (excludes password_hash)."""
    rows = conn.execute(
        "SELECT id, username, email, role, created_at, last_login_at FROM users ORDER BY created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def create_invite_code(conn, code, created_by, max_uses=1, expires_at=None):
    """Create an invite code."""
    conn.execute(
        "INSERT INTO invite_codes (code, created_by, max_uses, expires_at) VALUES (?, ?, ?, ?)",
        (code, created_by, max_uses, expires_at)
    )
    conn.commit()


def get_invite_code(conn, code):
    """Get invite code record."""
    row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
    return dict(row) if row else None


def use_invite_code(conn, code, user_id):
    """Increment used_count and record user_id in used_by JSON array."""
    cur = conn.execute(
        "UPDATE invite_codes SET used_count = used_count + 1 WHERE code = ? AND used_count < max_uses",
        (code,),
    )
    if cur.rowcount <= 0:
        conn.commit()
        return False
    row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
    used_by = json.loads(row['used_by']) if row['used_by'] else []
    used_by.append(user_id)
    conn.execute(
        "UPDATE invite_codes SET used_by = ? WHERE code = ?",
        (json.dumps(used_by), code)
    )
    conn.commit()
    return True


def list_invite_codes(conn):
    """List all invite codes."""
    rows = conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def delete_invite_code(conn, code):
    conn.execute("DELETE FROM invite_codes WHERE code = ?", (code,))
    conn.commit()


def create_session(conn, session_id, user_id, token_type, expires_at):
    """Create a JWT session record (for revocation tracking)."""
    conn.execute(
        "INSERT INTO sessions (id, user_id, token_type, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, token_type, expires_at)
    )
    conn.commit()


def get_session(conn, session_id):
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def delete_session(conn, session_id):
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def delete_user_sessions(conn, user_id):
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def cleanup_expired_sessions(conn):
    """Remove expired sessions."""
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    conn.commit()


# ── User Profiles (v12.0) ──

def get_user_profile(conn, user_id):
    """Get user profile. Returns dict or None."""
    row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    # Parse JSON fields
    for field in ('interests', 'tools'):
        if d.get(field) and isinstance(d[field], str):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


_SENTINEL = object()

def upsert_user_profile(conn, user_id, role=_SENTINEL, interests=_SENTINEL, tools=_SENTINEL, manifest=_SENTINEL, onboarding_completed=_SENTINEL):
    """Create or update user profile.

    Uses a sentinel default so callers can explicitly pass None to clear a field.
    Only fields that are explicitly passed (including None) are updated.
    """
    now = datetime.now().isoformat()

    existing = get_user_profile(conn, user_id)
    if existing:
        # Build dynamic UPDATE — only touch fields that were explicitly passed
        sets = ['updated_at = ?']
        params = [now]
        if role is not _SENTINEL:
            sets.append('role = ?'); params.append(role)
        if interests is not _SENTINEL:
            val = json.dumps(interests, ensure_ascii=False) if isinstance(interests, list) else interests
            sets.append('interests = ?'); params.append(val)
        if tools is not _SENTINEL:
            val = json.dumps(tools, ensure_ascii=False) if isinstance(tools, list) else tools
            sets.append('tools = ?'); params.append(val)
        if manifest is not _SENTINEL:
            sets.append('manifest = ?'); params.append(manifest)
        if onboarding_completed is not _SENTINEL:
            sets.append('onboarding_completed = ?'); params.append(1 if onboarding_completed else 0)
        params.append(user_id)
        conn.execute(f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = ?", params)
    else:
        # INSERT new row — sentinel fields become NULL / default
        r = role if role is not _SENTINEL else None
        i = interests if interests is not _SENTINEL else None
        t = tools if tools is not _SENTINEL else None
        m = manifest if manifest is not _SENTINEL else None
        o = 1 if (onboarding_completed is not _SENTINEL and onboarding_completed) else 0
        interests_json = json.dumps(i, ensure_ascii=False) if isinstance(i, list) else i
        tools_json = json.dumps(t, ensure_ascii=False) if isinstance(t, list) else t
        conn.execute("""
            INSERT INTO user_profiles (user_id, role, interests, tools, manifest, onboarding_completed, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, r, interests_json, tools_json, m, o, now, now))
    conn.commit()
    return get_user_profile(conn, user_id)


# ==================== v13.0: ASR 配额管理 ====================
# 每日 10h/天(可配 env `ASR_DAILY_QUOTA_HOURS`),按北京时间零点重置。
# - 单用户场景:user_id=0 占位
# - 手动触发(`bypass_quota=True`)也 **计入** 累计秒数,但不 **拦截**
# - SQLite 并发安全:UPDATE 走 `BEGIN IMMEDIATE` 事务(RESEARCH.md §1.4 决策)

ASR_DAILY_QUOTA_HOURS_DEFAULT = 10.0


def _asr_today_cst() -> str:
    """返回北京时间今天的 'YYYY-MM-DD'。

    BF-0419-7 已证明服务器实际是北京时间,这里用 `_LOCAL_TZ_OFFSET_HOURS`
    (-8 表示服务器本地 +8 区)把 UTC 投回本地日期。

    注意:如果未来服务器迁 UTC,`_LOCAL_TZ_OFFSET_HOURS` 改 0 后,本函数自然跟进。
    """
    from datetime import datetime, timedelta, timezone
    now_utc = datetime.now(timezone.utc)
    # _LOCAL_TZ_OFFSET_HOURS = -8 意思是 "UTC - (-8) = UTC+8 = 北京时间"
    cst = now_utc - timedelta(hours=_LOCAL_TZ_OFFSET_HOURS)
    return cst.strftime('%Y-%m-%d')


def _asr_daily_quota_sec() -> int:
    hours = float(os.environ.get('ASR_DAILY_QUOTA_HOURS', ASR_DAILY_QUOTA_HOURS_DEFAULT))
    return int(hours * 3600)


def get_asr_usage_today(conn, user_id: int = 0) -> dict:
    """返回今日 ASR 使用情况。

    Returns:
        {
            'date_cst':        '2026-04-19',
            'seconds_used':    11520,        # 累计秒
            'used_hours':      3.2,          # round 1
            'daily_quota_sec': 36000,        # 10h
            'remaining_hours': 6.8,
            'over_limit':      False,
            'reset_at':        'YYYY-MM-DDT00:00:00+08:00',  # 明日北京时间零点
        }
    """
    from datetime import datetime, timedelta
    today = _asr_today_cst()
    daily_sec = _asr_daily_quota_sec()
    row = conn.execute(
        "SELECT seconds_used FROM asr_usage WHERE user_id = ? AND date_cst = ?",
        (user_id, today),
    ).fetchone()
    used_sec = int(row['seconds_used']) if row and row['seconds_used'] else 0
    used_hours = round(used_sec / 3600, 1)
    remaining_sec = daily_sec - used_sec
    remaining_hours = round(remaining_sec / 3600, 1)
    # 明日 00:00 北京时间 = today + 1 天 @ 北京
    try:
        next_day = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    return {
        'date_cst': today,
        'seconds_used': used_sec,
        'used_hours': used_hours,
        'daily_quota_sec': daily_sec,
        'remaining_hours': remaining_hours,
        'over_limit': used_sec >= daily_sec,
        'reset_at': reset_at,
    }


def check_asr_quota(conn, duration_sec: int, user_id: int = 0) -> tuple[bool, dict]:
    """检查是否足够配额消费 duration_sec 秒。

    Returns:
        (allowed, usage_dict):
            - allowed: True 表示 used + duration_sec <= daily_sec
            - usage_dict: 同 get_asr_usage_today
    """
    usage = get_asr_usage_today(conn, user_id)
    allowed = (usage['seconds_used'] + max(0, int(duration_sec))) <= usage['daily_quota_sec']
    return allowed, usage


def consume_asr_quota(conn, duration_sec: int, user_id: int = 0) -> dict:
    """扣减配额(UPSERT asr_usage)。

    手动触发 `bypass_quota=True` 路径也应调本函数 —— 仅"扣减"不"拦截"。
    用 BEGIN IMMEDIATE 获取写锁,避免 ingest 并发 / 用户手动并行时读-读-写竞态。

    Returns: 更新后的 get_asr_usage_today 快照。
    """
    if duration_sec is None or duration_sec <= 0:
        return get_asr_usage_today(conn, user_id)
    from datetime import datetime
    today = _asr_today_cst()
    now_iso = datetime.now().isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # 已处于事务中(caller 自控),不嵌套 BEGIN
        pass
    try:
        conn.execute("""
            INSERT INTO asr_usage (user_id, date_cst, seconds_used, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, date_cst) DO UPDATE SET
                seconds_used = seconds_used + excluded.seconds_used,
                updated_at = excluded.updated_at
        """, (user_id, today, int(duration_sec), now_iso))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    return get_asr_usage_today(conn, user_id)


# ── v21.0 action-revival: 行动点生成每日配额 ──────────────────────

ACTION_GEN_DAILY_LIMIT_DEFAULT = 5


def action_gen_daily_limit() -> int:
    """每日生成上限(非 admin)。env `ACTION_GEN_DAILY_LIMIT` 可覆盖。"""
    raw = os.environ.get('ACTION_GEN_DAILY_LIMIT', '').strip()
    if not raw:
        return ACTION_GEN_DAILY_LIMIT_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return ACTION_GEN_DAILY_LIMIT_DEFAULT


def get_generation_usage_today(conn, user_id: str) -> dict:
    """返回某用户今日(北京时间)的生成配额快照。

    Returns:
        {
            'day_cst': '2026-07-04',
            'used': 2,
            'limit': 5,
            'remaining': 3,
            'over_limit': False,
            'reset_at': 'YYYY-MM-DDT00:00:00+08:00',
        }
    """
    from datetime import datetime, timedelta
    today = _asr_today_cst()
    limit = action_gen_daily_limit()
    row = conn.execute(
        "SELECT count FROM user_daily_generation WHERE user_id = ? AND day_cst = ?",
        (str(user_id), today),
    ).fetchone()
    used = int(row['count']) if row and row['count'] else 0
    try:
        next_day = (datetime.strptime(today, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    return {
        'day_cst': today,
        'used': used,
        'limit': limit,
        'remaining': max(0, limit - used),
        'over_limit': used >= limit,
        'reset_at': reset_at,
    }


def try_consume_generation_quota(conn, user_id: str) -> tuple[bool, dict]:
    """原子地"发起即计":若今日未超限则 +1 并返回 (True, 新快照),否则 (False, 当前快照)。

    用 BEGIN IMMEDIATE 拿写锁,避免并发多次生成刷额度的读-读-写竞态。
    admin 豁免由调用方(路由层)判断,本函数只管计数。
    """
    from datetime import datetime
    today = _asr_today_cst()
    limit = action_gen_daily_limit()
    now_iso = datetime.now().isoformat()
    own_tx = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        own_tx = True
    except sqlite3.OperationalError:
        pass
    try:
        row = conn.execute(
            "SELECT count FROM user_daily_generation WHERE user_id = ? AND day_cst = ?",
            (str(user_id), today),
        ).fetchone()
        used = int(row['count']) if row and row['count'] else 0
        if used >= limit:
            if own_tx:
                conn.rollback()
            return False, get_generation_usage_today(conn, user_id)
        conn.execute("""
            INSERT INTO user_daily_generation (user_id, day_cst, count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, day_cst) DO UPDATE SET
                count = count + 1,
                updated_at = excluded.updated_at
        """, (str(user_id), today, now_iso))
        if own_tx:
            conn.commit()
    except Exception:
        try:
            if own_tx:
                conn.rollback()
        except Exception:
            pass
        raise
    return True, get_generation_usage_today(conn, user_id)
