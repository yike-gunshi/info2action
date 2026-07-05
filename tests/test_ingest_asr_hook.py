"""v13.0: ingest_twitter ASR 钩子单元测试 — 不连网络,仅测分支。"""
import json
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr("db.DB_PATH", tmp.name)
    import db as _db
    _db._item_status_has_user_id = None
    yield tmp.name
    try: os.unlink(tmp.name)
    except OSError: pass


def _insert(conn, tid, asr_status=None):
    conn.execute(
        "INSERT INTO items (id, platform, source, title, content, url, media_json, "
        "asr_status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, "twitter", "test", "t", "c", "u",
         json.dumps([{"type": "video", "url": "http://fake"}]),
         asr_status, datetime.now().isoformat())
    )
    conn.commit()


def test_no_api_key_silent_skip(tmp_db, monkeypatch, capsys):
    monkeypatch.delenv("DOUBAO_ASR_API_KEY", raising=False)
    import db, ingest
    conn = db.get_conn()
    _insert(conn, "t1")
    ingest._run_asr_for_twitter_videos_inline(conn, ["t1"])
    out = capsys.readouterr().out
    assert "DOUBAO_ASR_API_KEY not set" in out
    # 没有抛异常 / 状态没变
    row = conn.execute("SELECT asr_status FROM items WHERE id=?", ("t1",)).fetchone()
    assert row["asr_status"] is None
    conn.close()


def test_empty_list_no_op(tmp_db, capsys):
    import db, ingest
    conn = db.get_conn()
    ingest._run_asr_for_twitter_videos_inline(conn, [])
    # 直接返回,无日志
    conn.close()


def test_all_already_processed_skip(tmp_db, monkeypatch, capsys):
    """所有 tweet 都已有 asr_status(非 NULL)→ skip,不调 run_asr_inline"""
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "fake")
    import db, ingest, asr_worker
    conn = db.get_conn()
    _insert(conn, "t1", asr_status="success")
    _insert(conn, "t2", asr_status="failed_asr")

    called = {"n": 0}
    monkeypatch.setattr(asr_worker, "run_asr_inline",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    ingest._run_asr_for_twitter_videos_inline(conn, ["t1", "t2"])
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "already processed" in out
    conn.close()


def test_only_pending_get_processed(tmp_db, monkeypatch, capsys):
    """asr_status=NULL 的才跑;已 success 的 skip"""
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "fake")
    monkeypatch.setenv("ASR_INGEST_CONCURRENCY", "1")
    import db, ingest, asr_worker
    from asr_worker import AsrResult
    conn = db.get_conn()
    _insert(conn, "done", asr_status="success")
    _insert(conn, "pending1")
    _insert(conn, "pending2")

    seen = []
    def fake_run(item_id, **kw):
        seen.append(item_id)
        return AsrResult("success", "x" * 30, 60, 0.01)
    monkeypatch.setattr(asr_worker, "run_asr_inline", fake_run)

    ingest._run_asr_for_twitter_videos_inline(conn, ["done", "pending1", "pending2"])
    assert set(seen) == {"pending1", "pending2"}
    conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
