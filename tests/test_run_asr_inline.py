"""v13.0: run_asr_inline 同步接口单元测试 — 无网络,只测分支。"""
import json
import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

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


@pytest.fixture
def asr_env(monkeypatch):
    """最小环境变量 mock"""
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "fake-key")


def _insert_item(conn, item_id, media_json=None):
    from datetime import datetime
    conn.execute(
        "INSERT INTO items (id, platform, source, title, content, url, media_json, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (item_id, "twitter", "test", "t", "c", "https://x.com/u/status/"+item_id,
         media_json, datetime.now().isoformat())
    )
    conn.commit()


def test_missing_api_key(tmp_db, monkeypatch):
    """DOUBAO_ASR_API_KEY 未设置 → RuntimeError"""
    monkeypatch.delenv("DOUBAO_ASR_API_KEY", raising=False)
    import db, asr_worker
    conn = db.get_conn()
    _insert_item(conn, "999", media_json=json.dumps([{"type": "video", "url": "http://x"}]))
    with pytest.raises(RuntimeError, match="DOUBAO_ASR_API_KEY"):
        asr_worker.run_asr_inline("999", conn=conn)
    conn.close()


def test_no_video_fails_empty(tmp_db, asr_env):
    """media_json 无 video → failed_empty"""
    import db, asr_worker
    conn = db.get_conn()
    _insert_item(conn, "111", media_json=json.dumps([{"type": "photo", "url": "http://x"}]))
    r = asr_worker.run_asr_inline("111", conn=conn)
    assert r.status == "failed_empty"
    # DB 也写了状态
    row = conn.execute("SELECT asr_status, asr_failed_reason FROM items WHERE id=?", ("111",)).fetchone()
    assert row["asr_status"] == "failed_empty"
    conn.close()


def test_local_mp3_missing(tmp_db, asr_env):
    """audio_source.local_mp3 路径不存在 → failed_download"""
    import db, asr_worker
    conn = db.get_conn()
    _insert_item(conn, "222")
    r = asr_worker.run_asr_inline(
        "222", conn=conn,
        audio_source={"local_mp3": "/tmp/nope-" + os.urandom(6).hex() + ".mp3"},
    )
    assert r.status == "failed_download"
    row = conn.execute("SELECT asr_status FROM items WHERE id=?", ("222",)).fetchone()
    assert row["asr_status"] == "failed_download"
    conn.close()


def test_no_audio_stream_fails_empty(tmp_db, asr_env, monkeypatch):
    """Twitter 视频没有音轨 → failed_empty,不暴露 ffmpeg 原始错误。"""
    import db, asr_worker
    conn = db.get_conn()
    _insert_item(conn, "223", media_json=json.dumps([{"type": "video", "url": "http://fake"}]))

    monkeypatch.setattr(asr_worker, "download_mp4", lambda u, d: (1024, 0.1))
    monkeypatch.setattr(
        asr_worker,
        "ffmpeg_extract_mp3",
        lambda *a, **k: (_ for _ in ()).throw(asr_worker.NoAudioStreamError("no audio stream in video")),
    )
    monkeypatch.setattr(
        asr_worker,
        "upload_to_oss",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("upload should not be called")),
    )

    r = asr_worker.run_asr_inline("223", conn=conn)

    assert r.status == "failed_empty"
    row = conn.execute("SELECT asr_status, asr_failed_reason FROM items WHERE id=?", ("223",)).fetchone()
    assert row["asr_status"] == "failed_empty"
    assert "no audio stream" in row["asr_failed_reason"]
    conn.close()


def test_skipped_quota(tmp_db, asr_env, monkeypatch):
    """配额已用尽 → skipped_quota(不调豆包),写 DB"""
    import db, asr_worker

    conn = db.get_conn()
    # 用掉 10h(default 配额) = 36000s
    db.consume_asr_quota(conn, 36000)
    _insert_item(conn, "333", media_json=json.dumps([{"type": "video", "url": "http://fake"}]))

    # mock download_mp4 + ffmpeg_extract_mp3 + ffprobe_duration,得到 duration
    monkeypatch.setattr(asr_worker, "download_mp4", lambda u, d: (1024, 0.1))
    monkeypatch.setattr(asr_worker, "ffmpeg_extract_mp3",
                        lambda a, b, **k: (open(b, "wb").write(b"x") and 1, 0.1))
    monkeypatch.setattr(asr_worker, "ffprobe_duration", lambda p: 60.0)
    # 不该走到 upload_to_oss — 如果被调用则测试失败
    monkeypatch.setattr(asr_worker, "upload_to_oss", lambda *a, **k: (_ for _ in ()).throw(AssertionError("upload should not be called")))

    r = asr_worker.run_asr_inline("333", bypass_quota=False, conn=conn)
    assert r.status == "skipped_quota"
    assert r.duration_sec == 60
    row = conn.execute("SELECT asr_status, asr_duration_sec FROM items WHERE id=?", ("333",)).fetchone()
    assert row["asr_status"] == "skipped_quota"
    assert row["asr_duration_sec"] == 60
    conn.close()


def test_bypass_quota_allows_when_over_limit(tmp_db, asr_env, monkeypatch):
    """bypass_quota=True:虽然配额超限,也要继续走流程(不应当提前退出)"""
    import db, asr_worker

    conn = db.get_conn()
    db.consume_asr_quota(conn, 36000)  # 10h 用尽
    _insert_item(conn, "444", media_json=json.dumps([{"type": "video", "url": "http://fake"}]))

    monkeypatch.setattr(asr_worker, "download_mp4", lambda u, d: (1024, 0.1))
    monkeypatch.setattr(asr_worker, "ffmpeg_extract_mp3",
                        lambda a, b, **k: (open(b, "wb").write(b"x") and 1, 0.1))
    monkeypatch.setattr(asr_worker, "ffprobe_duration", lambda p: 60.0)

    # mock upload + submit 走到 "被调用到就 ok",后续 poll 超时让它快速退出
    called = {"upload": False}
    def fake_upload(*a, **k):
        called["upload"] = True
        return ("https://signed/url", "key", 0.1)
    monkeypatch.setattr(asr_worker, "upload_to_oss", fake_upload)
    monkeypatch.setattr(asr_worker, "doubao_submit", lambda *a, **k: (None, {"msg": "fake fail"}))

    r = asr_worker.run_asr_inline("444", bypass_quota=True, conn=conn)
    # 未被 skipped_quota 拦住:应当走到 submit/upload 逻辑
    assert called["upload"] is True
    assert r.status == "failed_asr"  # submit 被我们 mock 为失败
    conn.close()


def test_run_asr_inline_checks_quota_for_triggering_user(tmp_db, asr_env, monkeypatch):
    """默认全局桶耗尽时,另一个用户自己的桶仍应可触发。"""
    import db, asr_worker

    conn = db.get_conn()
    db.consume_asr_quota(conn, 36000, user_id=0)
    _insert_item(conn, "555", media_json=json.dumps([{"type": "video", "url": "http://fake"}]))

    monkeypatch.setattr(asr_worker, "download_mp4", lambda u, d: (1024, 0.1))
    monkeypatch.setattr(asr_worker, "ffmpeg_extract_mp3",
                        lambda a, b, **k: (open(b, "wb").write(b"x") and 1, 0.1))
    monkeypatch.setattr(asr_worker, "ffprobe_duration", lambda p: 60.0)

    called = {"upload": False}

    def fake_upload(*a, **k):
        called["upload"] = True
        return ("https://signed/url", "key", 0.1)

    monkeypatch.setattr(asr_worker, "upload_to_oss", fake_upload)
    monkeypatch.setattr(asr_worker, "doubao_submit", lambda *a, **k: (None, {"msg": "fake fail"}))

    r = asr_worker.run_asr_inline("555", bypass_quota=False, conn=conn, user_id="user-2")

    assert called["upload"] is True
    assert r.status == "failed_asr"
    conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
