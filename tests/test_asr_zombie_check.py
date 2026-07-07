"""BF-0705-4: ASR 僵尸判定时区缺陷回归测试.

三层根因:写入本地 naive / remote 规范化假设 +8 / 读取裸 fromisoformat(3.10 不认 Z).
约定:asr_attempted_at 统一 UTC `YYYY-MM-DDTHH:MM:SSZ`;判定走 time_utils.parse_datetime.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import asr_worker
import db as db_mod
from routes.asr import _zombie_check


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """独立 sqlite,钉死本地模式(进程 env 优先于项目 .env)."""
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "sqlite")
    db_path = tmp_path / "test_feed.db"
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    conn = db_mod.get_conn()
    conn.execute(
        "INSERT INTO items(id, platform, source, title, fetched_at) "
        "VALUES ('t_z1', 'twitter', 'following', 'z', '2026-07-05T00:00:00')"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def la_timezone(monkeypatch):
    """模拟 UTC-7/-8 机器(BF-0705-1 QA 实测踩坑环境)."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def _state(conn, item_id="t_z1"):
    row = conn.execute(
        "SELECT asr_status, asr_attempted_at FROM items WHERE id=?", (item_id,)
    ).fetchone()
    return {"asr_status": row[0], "asr_attempted_at": row[1]}


class TestZombieCheckTimezone:
    def test_fresh_running_not_zombied_on_non_utc_machine(self, test_db, la_timezone):
        """刚写入 running 的任务,在 UTC-7 机器上不得被秒判僵尸(红:现状恒降级)."""
        asr_worker._write_asr_status(test_db, "t_z1", asr_status="running")
        state = _zombie_check(test_db, "t_z1", _state(test_db))
        assert state["asr_status"] == "running", (
            f"刚写入的 running 被误判为 {state['asr_status']}:"
            f"{state.get('asr_failed_reason')}(时区缺陷)"
        )

    def test_true_zombie_still_detected(self, test_db):
        """40min 前的 running(规范 Z 格式)必须仍被降级 worker_timeout."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        test_db.execute(
            "UPDATE items SET asr_status='running', asr_attempted_at=? WHERE id='t_z1'",
            (old,))
        test_db.commit()
        state = _zombie_check(test_db, "t_z1", _state(test_db))
        assert state["asr_status"] == "failed_asr"
        assert state["asr_failed_reason"] == "worker_timeout"

    def test_z_suffix_fresh_running_kept(self, test_db):
        """5min 前的 Z 格式 running 不降级(且解析路径必须兼容 py3.10:不依赖 fromisoformat 认 Z)."""
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        test_db.execute(
            "UPDATE items SET asr_status='running', asr_attempted_at=? WHERE id='t_z1'",
            (recent,))
        test_db.commit()
        state = _zombie_check(test_db, "t_z1", _state(test_db))
        assert state["asr_status"] == "running"

    def test_write_asr_status_writes_utc_z_format(self, test_db, la_timezone):
        """_write_asr_status 落库的 asr_attempted_at 必须是 UTC Z 规范格式(红:现状本地 naive)."""
        asr_worker._write_asr_status(test_db, "t_z1", asr_status="running")
        raw = test_db.execute(
            "SELECT asr_attempted_at FROM items WHERE id='t_z1'").fetchone()[0]
        assert raw.endswith("Z"), f"期望 UTC Z 格式,实际 {raw!r}"
        from time_utils import parse_datetime
        dt = parse_datetime(raw)
        assert abs((datetime.now(timezone.utc) - dt).total_seconds()) < 10, (
            f"时间戳偏离当前 UTC 超 10s: {raw!r}(时区污染)"
        )
