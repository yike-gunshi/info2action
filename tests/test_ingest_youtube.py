"""v13.0: ingest_youtube_url 单元测试 — 全 mock,无网络。"""
import json
import os
import sys
import tempfile
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


def _mock_meta():
    return {
        "title": "AI Future",
        "duration": 600,
        "uploader": "Some Channel",
        "thumbnail": "https://i.ytimg.com/vi/xxx.jpg",
    }


def test_english_subtitles_path(tmp_db, monkeypatch):
    """FEATURE-SPEC R3.1:英文字幕 → asr_provider='youtube_transcript_api+minimax'"""
    import db, ingest
    monkeypatch.setattr(ingest, "_youtube_fetch_metadata", lambda url: _mock_meta())

    segs = [{"start_ms": 0, "end_ms": 3000, "text": "Hello world"}]
    monkeypatch.setattr(ingest, "_youtube_try_transcript",
                        lambda vid: (segs, "en"))
    # mock translate
    import asr_worker
    monkeypatch.setattr(asr_worker, "translate_segments_cn",
                        lambda s: ["你好世界"])

    conn = db.get_conn()
    r = ingest.ingest_youtube_url(conn, "yt_abc123DEFxy", "https://youtu.be/abc123DEFxy")
    assert r["status"] == "ok"
    assert r["asr_status"] == "success"
    assert r["asr_provider"] == "youtube_transcript_api+minimax"
    row = conn.execute(
        "SELECT platform, title, asr_text, asr_segments, asr_segments_cn, asr_provider, "
        "asr_cost_yuan, asr_status FROM items WHERE id=?", ("yt_abc123DEFxy",)
    ).fetchone()
    assert row["platform"] == "youtube"
    assert row["title"] == "AI Future"
    assert row["asr_text"] == "Hello world"
    assert row["asr_provider"] == "youtube_transcript_api+minimax"
    assert row["asr_cost_yuan"] == 0  # 字幕路径免费(FEATURE-SPEC F5)
    # segments_cn 是 JSON
    cn_list = json.loads(row["asr_segments_cn"])
    assert cn_list == ["你好世界"]
    conn.close()


def test_chinese_subtitles_no_translate(tmp_db, monkeypatch):
    """FEATURE-SPEC R3.2:中文字幕 → asr_segments_cn NULL(不二次翻译)"""
    import db, ingest, asr_worker
    monkeypatch.setattr(ingest, "_youtube_fetch_metadata", lambda url: _mock_meta())
    segs = [{"start_ms": 0, "end_ms": 3000, "text": "你好世界"}]
    monkeypatch.setattr(ingest, "_youtube_try_transcript",
                        lambda vid: (segs, "zh-CN"))

    called = {"translate": 0}
    def _translate(_):
        called["translate"] += 1
        return ["x"]
    monkeypatch.setattr(asr_worker, "translate_segments_cn", _translate)

    conn = db.get_conn()
    r = ingest.ingest_youtube_url(conn, "yt_zh00000000z", "https://youtu.be/zh00000000z")
    assert r["status"] == "ok"
    assert r["asr_provider"] == "youtube_transcript_api"  # 无 +minimax
    # 不应调翻译
    assert called["translate"] == 0
    row = conn.execute("SELECT asr_segments_cn FROM items WHERE id=?",
                       ("yt_zh00000000z",)).fetchone()
    assert row["asr_segments_cn"] is None
    conn.close()


def test_zh_hans_also_skips_translate(tmp_db, monkeypatch):
    """lang='zh-Hans' 也判中文"""
    import db, ingest, asr_worker
    monkeypatch.setattr(ingest, "_youtube_fetch_metadata", lambda url: _mock_meta())
    monkeypatch.setattr(ingest, "_youtube_try_transcript",
                        lambda vid: ([{"start_ms": 0, "end_ms": 1000, "text": "测试"}], "zh-Hans"))
    called = {"n": 0}
    monkeypatch.setattr(asr_worker, "translate_segments_cn",
                        lambda s: (called.__setitem__("n", called["n"] + 1), ["x"])[1])

    conn = db.get_conn()
    ingest.ingest_youtube_url(conn, "yt_zhHans00001", "https://youtu.be/zhHans00001")
    assert called["n"] == 0
    conn.close()


def test_metadata_failure_no_db_write(tmp_db, monkeypatch):
    """FEATURE-SPEC R4.3:yt-dlp 抛异常 → item MUST NOT 入库"""
    import db, ingest
    def boom(url): raise RuntimeError("proxy down")
    monkeypatch.setattr(ingest, "_youtube_fetch_metadata", boom)

    conn = db.get_conn()
    r = ingest.ingest_youtube_url(conn, "yt_fail0000001", "https://youtu.be/fail0000001")
    assert r["status"] == "error"
    # BF-0419-16: 文案从 "yt-dlp metadata failed" 改为用户友好中文
    # 未匹配代理/登录/unavailable 特殊分支时,走 fallback "YouTube 元数据抓取失败"
    assert "YouTube 元数据抓取失败" in r["error"]
    assert "RuntimeError" in r["error"]  # type 保留,便于排查
    row = conn.execute("SELECT * FROM items WHERE id=?",
                       ("yt_fail0000001",)).fetchone()
    assert row is None
    conn.close()


def test_metadata_proxy_error_friendly_message(monkeypatch, tmp_db):
    """BF-0419-16: 代理连接失败抛出的错误应被替换为用户友好文案。"""
    import db, ingest
    def proxy_fail(url):
        raise RuntimeError(
            "ERROR: [youtube] xxx: Unable to download API page: ('Unable to connect to proxy', "
            "NewConnectionError(\"HTTPSConnection(host='127.0.0.1', port=7890)\"))"
        )
    monkeypatch.setattr(ingest, "_youtube_fetch_metadata", proxy_fail)

    conn = db.get_conn()
    r = ingest.ingest_youtube_url(conn, "yt_proxy00001", "https://youtu.be/proxy00001")
    assert r["status"] == "error"
    assert "代理" in r["error"] and "Clash" in r["error"]
    assert "127.0.0.1:7890" in r["error"]
    # 不应泄漏 NewConnectionError 等内部异常名给用户
    assert "NewConnectionError" not in r["error"]
    conn.close()


def test_cues_with_empty_text_skipped(tmp_db):
    """cues 里空 text 段要被跳过"""
    import ingest
    cues = [
        {"text": "hello", "start": 0.0, "duration": 2.0},
        {"text": "", "start": 2.0, "duration": 1.0},
        {"text": " world ", "start": 3.0, "duration": 1.5},
    ]
    segs = ingest._youtube_build_segments_from_cues(cues)
    assert len(segs) == 2
    assert segs[0] == {"start_ms": 0, "end_ms": 2000, "text": "hello"}
    assert segs[1] == {"start_ms": 3000, "end_ms": 4500, "text": "world"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
