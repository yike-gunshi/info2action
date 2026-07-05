#!/usr/bin/env python3
"""独立的用户反馈数据库 — 与工程框架解耦，用于推荐系统迭代。

数据库文件: data/user_feedback.db
可独立导出、分析，不依赖 feed.db 或任何服务。
"""
import json, os, sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FB_DB_PATH = os.path.join(BASE, 'data', 'user_feedback.db')

SCHEMA = """
-- 帖子级反馈（冗余存储帖子信息，脱离工程框架也能理解）
CREATE TABLE IF NOT EXISTS item_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  platform TEXT,
  item_title TEXT,
  item_author TEXT,
  item_url TEXT,
  action TEXT NOT NULL,          -- positive / irrelevant / low_quality
  reason TEXT,                   -- 用户自然语言描述
  topic_at_time TEXT,            -- 当时被归到哪个 topic
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_if_item ON item_feedback(item_id);
CREATE INDEX IF NOT EXISTS idx_if_action ON item_feedback(action);
CREATE INDEX IF NOT EXISTS idx_if_created ON item_feedback(created_at);

-- 系统级反馈（趋势词不好、分类不准等）
CREATE TABLE IF NOT EXISTS system_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT,                 -- trend / classification / ui / content / other
  description TEXT,              -- 自然语言描述
  context_json TEXT,             -- 相关上下文快照（可选）
  created_at TEXT DEFAULT (datetime('now'))
);

-- 偏好学习记录
CREATE TABLE IF NOT EXISTS preference_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_type TEXT,              -- author_like / author_dislike / topic_interest / keyword_block
  target TEXT,                   -- 对应的作者名/话题/关键词
  note TEXT,                     -- 用户备注
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ps_type ON preference_signals(signal_type);
CREATE INDEX IF NOT EXISTS idx_ps_target ON preference_signals(target);
"""


def get_conn():
    """Get a connection to the feedback database."""
    os.makedirs(os.path.dirname(FB_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(FB_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def record_item_feedback(conn, item_id, action, platform=None, title=None,
                         author=None, url=None, reason=None, topic=None):
    """记录一条帖子级反馈。"""
    conn.execute("""
        INSERT INTO item_feedback
            (item_id, platform, item_title, item_author, item_url, action, reason, topic_at_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (item_id, platform, title, author, url, action, reason, topic))
    conn.commit()


def record_system_feedback(conn, category, description, context=None):
    """记录一条系统级反馈。"""
    ctx = json.dumps(context, ensure_ascii=False) if context else None
    conn.execute("""
        INSERT INTO system_feedback (category, description, context_json)
        VALUES (?, ?, ?)
    """, (category, description, ctx))
    conn.commit()


def record_preference(conn, signal_type, target, note=None):
    """记录一条偏好信号。"""
    conn.execute("""
        INSERT INTO preference_signals (signal_type, target, note)
        VALUES (?, ?, ?)
    """, (signal_type, target, note))
    conn.commit()


def get_all_item_feedback(conn, limit=500):
    """导出所有帖子反馈。"""
    rows = conn.execute(
        "SELECT * FROM item_feedback ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_preferences(conn):
    """导出所有偏好信号。"""
    rows = conn.execute(
        "SELECT * FROM preference_signals ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_feedback_summary(conn):
    """获取反馈统计摘要。"""
    stats = {}
    for action in ('positive', 'irrelevant', 'low_quality'):
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM item_feedback WHERE action=?", (action,)
        ).fetchone()
        stats[action] = row['cnt']

    row = conn.execute("SELECT COUNT(*) as cnt FROM system_feedback").fetchone()
    stats['system'] = row['cnt']

    row = conn.execute("SELECT COUNT(*) as cnt FROM preference_signals").fetchone()
    stats['preferences'] = row['cnt']

    return stats
