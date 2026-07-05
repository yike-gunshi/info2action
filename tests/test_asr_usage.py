"""v13.0: ASR 配额表 + 3 个函数单元测试"""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch):
    """每个测试用独立的 SQLite 临时文件。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr("db.DB_PATH", tmp.name)
    # 重置 cache(PRAGMA table_info 的缓存)
    import db as _db
    _db._item_status_has_user_id = None
    yield tmp.name
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def test_asr_usage_table_created(tmp_db):
    import db
    conn = db.get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='asr_usage'"
    ).fetchone()
    assert row is not None
    # Columns
    cols = {r[1] for r in conn.execute("PRAGMA table_info(asr_usage)").fetchall()}
    assert cols == {"user_id", "date_cst", "seconds_used", "updated_at"}
    conn.close()


def test_idempotent_schema(tmp_db):
    """重复建表幂等(FEATURE-SPEC §5.3 P6)"""
    import db
    c1 = db.get_conn(); c1.close()
    c2 = db.get_conn(); c2.close()  # 不应报错


def test_get_asr_usage_today_empty(tmp_db):
    """零态:无记录返回 0 + 10h 余额"""
    import db
    conn = db.get_conn()
    u = db.get_asr_usage_today(conn, user_id=0)
    assert u['seconds_used'] == 0
    assert u['used_hours'] == 0.0
    assert u['remaining_hours'] == 10.0  # default 10h
    assert u['daily_quota_sec'] == 36000
    assert u['over_limit'] is False
    assert u['reset_at'] is not None
    conn.close()


def test_consume_and_readback(tmp_db):
    """扣减 3600s 后读回 1h"""
    import db
    conn = db.get_conn()
    u = db.consume_asr_quota(conn, 3600, user_id=0)
    assert u['seconds_used'] == 3600
    assert u['used_hours'] == 1.0
    assert u['remaining_hours'] == 9.0
    # 再扣 1h
    u2 = db.consume_asr_quota(conn, 3600, user_id=0)
    assert u2['seconds_used'] == 7200
    assert u2['used_hours'] == 2.0
    conn.close()


def test_check_asr_quota_allowed(tmp_db):
    """余额足够 → allowed=True"""
    import db
    conn = db.get_conn()
    allowed, u = db.check_asr_quota(conn, 60, user_id=0)
    assert allowed is True
    assert u['seconds_used'] == 0
    conn.close()


def test_check_asr_quota_denied(tmp_db):
    """累计到 9h 后再申请 2h → allowed=False"""
    import db
    conn = db.get_conn()
    db.consume_asr_quota(conn, 9 * 3600, user_id=0)
    allowed, u = db.check_asr_quota(conn, 2 * 3600, user_id=0)
    assert allowed is False
    assert u['used_hours'] == 9.0
    conn.close()


def test_over_limit_flag(tmp_db):
    """累计超过 daily_sec → over_limit=True, remaining_hours<0"""
    import db
    conn = db.get_conn()
    db.consume_asr_quota(conn, 11 * 3600, user_id=0)  # 11h
    u = db.get_asr_usage_today(conn, user_id=0)
    assert u['over_limit'] is True
    assert u['remaining_hours'] == -1.0
    assert u['used_hours'] == 11.0
    conn.close()


def test_user_id_shard(tmp_db):
    """多用户隔离(预留):user_id=0 和 user_id=1 互不影响"""
    import db
    conn = db.get_conn()
    db.consume_asr_quota(conn, 3600, user_id=0)
    db.consume_asr_quota(conn, 7200, user_id=1)
    u0 = db.get_asr_usage_today(conn, user_id=0)
    u1 = db.get_asr_usage_today(conn, user_id=1)
    assert u0['seconds_used'] == 3600
    assert u1['seconds_used'] == 7200
    conn.close()


def test_zero_consume_noop(tmp_db):
    """consume 0 秒:幂等,不建记录"""
    import db
    conn = db.get_conn()
    u = db.consume_asr_quota(conn, 0, user_id=0)
    assert u['seconds_used'] == 0
    row = conn.execute(
        "SELECT COUNT(*) as c FROM asr_usage WHERE user_id=0"
    ).fetchone()
    assert row['c'] == 0
    conn.close()


def test_env_quota_override(tmp_db, monkeypatch):
    """ASR_DAILY_QUOTA_HOURS env var 覆盖默认 10h"""
    import db
    monkeypatch.setenv('ASR_DAILY_QUOTA_HOURS', '5')
    conn = db.get_conn()
    u = db.get_asr_usage_today(conn, user_id=0)
    assert u['daily_quota_sec'] == 5 * 3600
    assert u['remaining_hours'] == 5.0
    conn.close()


def test_begin_immediate_concurrent(tmp_db):
    """两个 conn 并发 consume,累计值正确(不丢失)"""
    import db
    conn1 = db.get_conn()
    conn2 = db.get_conn()
    db.consume_asr_quota(conn1, 1800, user_id=0)
    db.consume_asr_quota(conn2, 1800, user_id=0)
    u = db.get_asr_usage_today(conn1, user_id=0)
    assert u['seconds_used'] == 3600
    conn1.close(); conn2.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
