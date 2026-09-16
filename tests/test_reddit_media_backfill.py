from __future__ import annotations

import json
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest


BASE = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "src"))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE items (
          id TEXT PRIMARY KEY,
          platform TEXT,
          url TEXT,
          cover_url TEXT,
          media_json TEXT,
          fetched_at TEXT
        )
        """
    )
    return conn


def test_count_missing_media_limits_to_recent_reddit_items():
    from scripts import reddit_media_backfill

    conn = _conn()
    conn.executemany(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("recent-missing", "reddit", "https://reddit.com/a", None, None, "2026-07-20T00:00:00Z"),
            ("recent-empty", "reddit", "https://reddit.com/b", "", "[]", "2026-07-19T00:00:00Z"),
            ("recent-media", "reddit", "https://reddit.com/c", "p", '[{"type":"video"}]', "2026-07-18T00:00:00Z"),
            ("old-missing", "reddit", "https://reddit.com/d", None, None, "2026-06-01T00:00:00Z"),
            ("other", "twitter", "https://x.com/e", None, None, "2026-07-20T00:00:00Z"),
        ],
    )

    assert reddit_media_backfill.count_missing_media(
        conn,
        days=30,
        now_iso="2026-07-24T00:00:00Z",
    ) == 2


def test_backfill_one_item_updates_only_requested_local_row():
    from scripts import reddit_media_backfill

    conn = _conn()
    conn.executemany(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("reddit_one", "reddit", "https://reddit.com/one", None, None, "2026-07-20T00:00:00Z"),
            ("reddit_two", "reddit", "https://reddit.com/two", None, None, "2026-07-20T00:00:00Z"),
        ],
    )
    media = [
        {
            "type": "video",
            "url": "https://v.redd.it/one.mp4",
            "poster_url": "https://preview.redd.it/one.jpg",
            "provider": "reddit",
            "source_url": "https://reddit.com/one",
        }
    ]

    result = reddit_media_backfill.backfill_one_item(
        conn,
        "reddit_one",
        media_loader=lambda _url: media,
    )

    assert result["status"] == "updated"
    row = conn.execute(
        "SELECT cover_url, media_json FROM items WHERE id='reddit_one'"
    ).fetchone()
    assert row["cover_url"] == media[0]["poster_url"]
    assert json.loads(row["media_json"]) == media
    untouched = conn.execute(
        "SELECT media_json FROM items WHERE id='reddit_two'"
    ).fetchone()
    assert untouched["media_json"] is None


def test_backfill_one_item_failure_is_non_blocking_and_does_not_write():
    from scripts import reddit_media_backfill

    conn = _conn()
    conn.execute(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)",
        ("reddit_one", "reddit", "https://reddit.com/one", None, None, "2026-07-20T00:00:00Z"),
    )

    result = reddit_media_backfill.backfill_one_item(
        conn,
        "reddit_one",
        media_loader=lambda _url: [],
    )

    assert result == {"item_id": "reddit_one", "status": "no_media"}
    row = conn.execute(
        "SELECT cover_url, media_json FROM items WHERE id='reddit_one'"
    ).fetchone()
    assert row["cover_url"] is None
    assert row["media_json"] is None


def test_remote_apply_requires_explicit_single_item_allowlist():
    from scripts import reddit_media_backfill

    missing_allowlist = SimpleNamespace(
        backend="remote",
        db=None,
        item_id="reddit_1v4ol30",
        allow_item_id=[],
        apply=True,
    )
    with pytest.raises(ValueError, match="--allow-item-id"):
        reddit_media_backfill.validate_args(missing_allowlist)

    allowed = SimpleNamespace(
        backend="remote",
        db=None,
        item_id="reddit_1v4ol30",
        allow_item_id=["reddit_1v4ol30"],
        apply=True,
    )
    reddit_media_backfill.validate_args(allowed)

    asr_without_apply = SimpleNamespace(
        backend="remote",
        db=None,
        item_id="reddit_1v4ol30",
        allow_item_id=["reddit_1v4ol30"],
        apply=False,
        run_asr=True,
    )
    with pytest.raises(ValueError, match="--run-asr requires --apply"):
        reddit_media_backfill.validate_args(asr_without_apply)


def test_backfill_asr_reuses_existing_resummary_chain(monkeypatch):
    from scripts import reddit_media_backfill
    import asr_worker

    seen = []
    monkeypatch.setattr(
        asr_worker,
        "run_asr_inline",
        lambda item_id, **kwargs: (
            seen.append((item_id, kwargs))
            or SimpleNamespace(status="success", transcript="spoken evidence")
        ),
    )

    result = reddit_media_backfill.run_item_asr("reddit_1v4ol30")

    assert result == {
        "item_id": "reddit_1v4ol30",
        "asr_status": "success",
        "has_transcript": True,
    }
    assert seen == [("reddit_1v4ol30", {"bypass_quota": False, "conn": None})]


def test_remote_backfill_updates_only_allowlisted_item():
    from scripts import reddit_media_backfill

    class Cursor:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

    class Conn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            if sql.lstrip().upper().startswith("SELECT"):
                return Cursor(
                    {
                        "id": "reddit_1v4ol30",
                        "platform": "reddit",
                        "url": "https://reddit.com/oversteer",
                    }
                )
            return Cursor()

    media = [
        {
            "type": "video",
            "url": "https://v.redd.it/oversteer.mp4",
            "poster_url": "https://preview.redd.it/oversteer.jpg",
        }
    ]
    conn = Conn()

    result = reddit_media_backfill.backfill_one_item_remote(
        conn,
        "reddit_1v4ol30",
        allowed_item_ids={"reddit_1v4ol30"},
        schema="remote_poc",
        media_loader=lambda _url: media,
    )

    assert result["status"] == "updated"
    updates = [call for call in conn.calls if call[0].upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert updates[0][1][-1] == "reddit_1v4ol30"

    with pytest.raises(ValueError, match="allowlist"):
        reddit_media_backfill.backfill_one_item_remote(
            conn,
            "reddit_other",
            allowed_item_ids={"reddit_1v4ol30"},
            schema="remote_poc",
            media_loader=lambda _url: media,
        )
