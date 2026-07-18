"""Ring 1: lingowhale incremental fetch — watermark early-stop.

Feature: incremental-fetch-15min (v20.0), R1 正常 + 边界(冷启动).
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fetch_lingowhale as lw  # noqa: E402


def _entry(pub_time, eid):
    return {"entry_id": eid, "title": f"t{eid}", "pub_time": pub_time,
            "channel": {"name": "x-公众号"}}


def _mock_pages(monkeypatch, pages):
    state = {"i": 0}

    def fake_page(endpoint, channel_ids, cursor, timeout=30):
        i = state["i"]
        state["i"] += 1
        return pages[i]

    monkeypatch.setattr(lw, "_fetch_feed_page", fake_page)
    return state


def test_watermark_stop_returns_only_newer_and_stops_early(monkeypatch):
    # newest-first pages; watermark (since_ts) = 75
    pages = [
        {"feed_list": [_entry(100, "a"), _entry(90, "b"), _entry(80, "c")],
         "has_more": True, "cursor": "c1"},
        {"feed_list": [_entry(70, "d"), _entry(60, "e")],
         "has_more": True, "cursor": "c2"},
        {"feed_list": [_entry(50, "f")], "has_more": False, "cursor": ""},
    ]
    state = _mock_pages(monkeypatch, pages)
    entries, page, reason = lw._fetch_subscription_feed_from_endpoint(
        "ep", ["ch"], "L", since_ts=75)
    got = sorted(e["pub_time"] for e in entries)
    assert got == [80, 90, 100], "只应保留 pub_time > 水位线的 entry"
    assert state["i"] == 2, "翻到旧内容(page2)即停,不应再翻 page3"
    assert "watermark" in reason


def test_parse_watermark_iso_to_unix():
    from datetime import datetime, timezone
    dt = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
    ts = lw._parse_watermark_to_ts(dt.isoformat())
    assert abs(ts - dt.timestamp()) < 1


def test_parse_watermark_failsafe_returns_none():
    # None / 空 / 垃圾 → None(fail-safe,回退冷启动全窗口)
    assert lw._parse_watermark_to_ts(None) is None
    assert lw._parse_watermark_to_ts("") is None
    assert lw._parse_watermark_to_ts("not-a-date") is None


def test_local_watermark_connection_is_closed(monkeypatch):
    import db
    import remote_db

    class FakeCursor:
        def fetchone(self):
            return {"mx": "2026-07-11T01:00:00Z"}

    class FakeConn:
        closed = False

        def execute(self, sql):
            assert "platform = 'lingowhale'" in sql
            return FakeCursor()

        def close(self):
            self.closed = True

    conn = FakeConn()
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(db, "get_conn", lambda: conn)

    assert lw._lingowhale_watermark_ts() is not None
    assert conn.closed is True


def test_fetch_subscription_feed_forwards_since_ts(monkeypatch):
    captured = []

    def fake_endpoint(endpoint, channel_ids, label, timeout=30, since_ts=None):
        captured.append(since_ts)
        return ([], 1, "done")

    monkeypatch.setenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", "0")
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"registered": 1})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_endpoint)
    monkeypatch.setattr(lw, "_record_lingowhale_result", lambda source_id, *, ok, error=None: None)
    lw.fetch_subscription_feed(groups_info=None, since_ts=12345)
    assert captured, "应至少调用一次注册表频道 endpoint"
    assert all(s == 12345 for s in captured), "since_ts 应透传到每个 endpoint 调用"


def test_cold_start_none_watermark_uses_lookback(monkeypatch):
    # since_ts=None → 回退到 lookback 窗口(冷启动),不因水位线早停
    import time
    now = time.time()
    pages = [
        {"feed_list": [_entry(now, "a"), _entry(now - 3600, "b")],
         "has_more": False, "cursor": ""},
    ]
    _mock_pages(monkeypatch, pages)
    entries, page, reason = lw._fetch_subscription_feed_from_endpoint(
        "ep", ["ch"], "L", since_ts=None)
    assert len(entries) == 2, "冷启动应按原 lookback 行为保留窗口内 entry"
    assert "watermark" not in reason
