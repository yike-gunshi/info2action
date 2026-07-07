"""W2 subscription-config: fetch active X user handles and ingest as Twitter items."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from types import SimpleNamespace

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
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _insert_source(conn, platform, source_key, *, status="active", name=None, config=None):
    conn.execute(
        "INSERT INTO sources(platform, source_key, display_name, status, config_json, origin) "
        "VALUES(?,?,?,?,?,?)",
        (
            platform,
            source_key,
            name,
            status,
            json.dumps(config, ensure_ascii=False) if config is not None else None,
            "test",
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM sources WHERE platform=? AND source_key=?",
        (platform, source_key),
    ).fetchone()[0]


def _source_row(conn, source_id):
    return conn.execute(
        """SELECT status, consecutive_failures, last_success_at, last_error
           FROM sources WHERE id = ?""",
        (source_id,),
    ).fetchone()


def _set_source_failures(conn, source_id, failures, *, error="old error"):
    conn.execute(
        """UPDATE sources
           SET consecutive_failures = ?, last_error = ?
           WHERE id = ?""",
        (failures, error, source_id),
    )
    conn.commit()


def _item(item_id, source):
    return {
        "id": item_id,
        "platform": "twitter",
        "source": source,
        "title": "tweet",
        "fetched_at": "2026-07-05T00:00:00Z",
    }


def test_active_x_handles_use_registry_and_skip_paused(tmp_db):
    import db
    import fetch_x_users

    conn = db.get_conn()
    _insert_source(conn, "x_user", "karpathy")
    _insert_source(conn, "x_user", "sama")
    _insert_source(conn, "x_user", "paused_user", status="paused")

    handles = fetch_x_users._active_x_handles(conn)

    assert handles == ["karpathy", "sama"]
    conn.close()


def test_active_x_sources_uses_remote_when_fetch_write_enabled(monkeypatch):
    import db
    import fetch_x_users
    import remote_db

    sentinel = [{"id": 201, "source_key": "remote_handle"}]

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(remote_db, "list_active_sources_remote", lambda platform: sentinel)
    monkeypatch.setattr(db, "get_conn", lambda: pytest.fail("opened local db"))
    monkeypatch.setattr(db, "list_active_sources", lambda conn, platform: pytest.fail("used local sources"))

    assert fetch_x_users._active_x_sources() is sentinel


def test_active_x_sources_uses_local_when_fetch_write_disabled(monkeypatch):
    import db
    import fetch_x_users
    import remote_db

    sentinel = [{"id": 202, "source_key": "local_handle"}]
    conn = object()

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(
        remote_db,
        "list_active_sources_remote",
        lambda platform: pytest.fail("used remote sources"),
    )

    def fake_list_active_sources(got_conn, platform):
        assert got_conn is conn
        assert platform == "x_user"
        return sentinel

    monkeypatch.setattr(db, "list_active_sources", fake_list_active_sources)

    assert fetch_x_users._active_x_sources(conn) is sentinel


def test_fetch_x_users_records_success_and_failure_per_handle(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 5}})
    conn = db.get_conn()
    good_id = _insert_source(conn, "x_user", "good")
    bad_id = _insert_source(conn, "x_user", "bad")
    _set_source_failures(conn, good_id, 3)
    _set_source_failures(conn, bad_id, 2)

    def fake_fetch_user_posts(handle, count):
        assert count == 5
        if handle == "bad":
            raise RuntimeError("boom")
        return [
            {
                "id": "101",
                "text": "hello",
                "time": "2026-07-06T00:00:00Z",
            }
        ]

    monkeypatch.setattr(fetch_x_users, "_fetch_user_posts", fake_fetch_user_posts)

    stats = fetch_x_users.fetch_x_users(conn, count=5, batch_size=10)

    assert stats == {"handles": 2, "ok": 1, "failed": 1}
    good_row = _source_row(conn, good_id)
    assert good_row["consecutive_failures"] == 0
    assert good_row["last_success_at"]
    assert good_row["last_error"] is None
    bad_row = _source_row(conn, bad_id)
    assert bad_row["consecutive_failures"] == 3
    assert bad_row["last_error"] == "boom"
    conn.close()


def test_fetch_x_users_continues_when_one_handle_fails(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 7}})
    conn = db.get_conn()
    _insert_source(conn, "x_user", "good")
    _insert_source(conn, "x_user", "bad")
    calls = []

    def fake_run(args, capture_output, text, timeout):
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert timeout == 60
        handle = args[3]
        if handle == "bad":
            return SimpleNamespace(returncode=1, stdout="", stderr="not found")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([
                {
                    "id": "tw1",
                    "author": "Good User",
                    "text": "hello",
                    "likes": 5,
                    "rts": 2,
                    "time": "2026-07-05T00:00:00Z",
                }
            ]),
            stderr="",
        )

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_x_users.time, "sleep", lambda seconds: None)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 2, "ok": 1, "failed": 1}
    assert calls[0] == ["twitter", "--compact", "user-posts", "good", "-n", "7"]
    assert calls[1:] == [["twitter", "--compact", "user-posts", "bad", "-n", "7"]] * 4
    out_path = tmp_path / "sources" / "twitter" / "x-user-good.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["handle"] == "good"
    assert payload["data"][0]["author"]["screenName"] == "good"
    assert not (tmp_path / "sources" / "twitter" / "x-user-bad.json").exists()
    conn.close()


def test_fetch_x_users_filters_posts_at_or_below_watermark(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 5}})
    conn = db.get_conn()
    source_id = _insert_source(conn, "x_user", "karpathy")
    conn.execute(
        """INSERT INTO items(id, platform, source, source_id, title, fetched_at, published_at)
           VALUES(?,?,?,?,?,?,?)""",
        (
            "tw_100",
            "twitter",
            "user:karpathy",
            source_id,
            "old",
            "2026-07-05T00:00:00Z",
            "2026-07-05T00:00:00Z",
        ),
    )
    conn.commit()

    def fake_run(args, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([
                {"id": "102", "text": "new", "time": "2026-07-05T00:02:00Z"},
                {"id": "100", "text": "same", "time": "2026-07-05T00:00:00Z"},
                {"id": "99", "text": "old", "time": "2026-07-04T23:59:00Z"},
            ]),
            stderr="",
        )

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 1, "ok": 1, "failed": 0}
    payload = json.loads((tmp_path / "sources" / "twitter" / "x-user-karpathy.json").read_text())
    assert [tweet["id"] for tweet in payload["data"]] == ["102"]
    conn.close()


def test_fetch_user_posts_retries_rate_limit_then_succeeds(monkeypatch):
    import fetch_x_users

    calls = []
    sleeps = []

    def fake_run(args, capture_output, text, timeout):
        calls.append(args)
        if len(calls) < 4:
            return SimpleNamespace(returncode=1, stdout="", stderr="429 rate limit")
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "101"}]), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_x_users.time, "sleep", sleeps.append)

    posts = fetch_x_users._fetch_user_posts("karpathy", 10)

    assert posts == [{"id": "101"}]
    assert len(calls) == 4
    assert sleeps == [1, 2, 4]


def test_fetch_x_users_skips_handle_after_retryable_failures(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 5}})
    conn = db.get_conn()
    _insert_source(conn, "x_user", "bad")
    _insert_source(conn, "x_user", "good")
    attempts = {}

    def fake_run(args, capture_output, text, timeout):
        handle = args[3]
        attempts[handle] = attempts.get(handle, 0) + 1
        if handle == "bad":
            return SimpleNamespace(returncode=1, stdout="", stderr="timeout")
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "101"}]), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_x_users.time, "sleep", lambda seconds: None)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 2, "ok": 1, "failed": 1}
    assert attempts == {"bad": 4, "good": 1}
    assert not (tmp_path / "sources" / "twitter" / "x-user-bad.json").exists()
    assert (tmp_path / "sources" / "twitter" / "x-user-good.json").exists()
    conn.close()


def test_fetch_x_users_rotates_batch_cursor(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {"user_posts_count": 3, "x_user_batch_size": 2},
    })
    conn = db.get_conn()
    for handle in ("a", "b", "c"):
        _insert_source(conn, "x_user", handle)
    fetched = []

    def fake_run(args, capture_output, text, timeout):
        handle = args[3]
        fetched.append(handle)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": f"{handle}1"}]), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)

    first_stats = fetch_x_users.fetch_x_users(conn)
    second_stats = fetch_x_users.fetch_x_users(conn)

    assert first_stats == {"handles": 3, "selected": 2, "ok": 2, "failed": 0}
    assert second_stats == {"handles": 3, "selected": 2, "ok": 2, "failed": 0}
    assert fetched == ["a", "b", "c", "a"]
    cursor_path = tmp_path / "sources" / "twitter" / ".x_user_cursor.json"
    assert json.loads(cursor_path.read_text()) == {"next_index": 1}
    conn.close()


def test_resolve_source_maps_twitter_user_source_and_keeps_algo_sources_null(tmp_db):
    import db

    conn = db.get_conn()
    active_id = _insert_source(conn, "x_user", "karpathy")
    paused_id = _insert_source(conn, "x_user", "paused_user", status="paused")
    idx = db.load_source_index(conn)

    assert db.resolve_source(idx, "twitter", "user:karpathy") == (active_id, "active")
    assert db.resolve_source(idx, "twitter", "user:paused_user") == (paused_id, "paused")
    assert db.resolve_source(idx, "twitter", "following") == (None, None)
    assert db.resolve_source(idx, "twitter", "for_you") == (None, None)

    assert db.upsert_item(conn, _item("tw_active", "user:karpathy"), source_index=idx) != "dropped"
    assert db.upsert_item(conn, _item("tw_paused", "user:paused_user"), source_index=idx) == "dropped"
    assert conn.execute("SELECT source_id FROM items WHERE id='tw_active'").fetchone()[0] == active_id
    assert conn.execute("SELECT 1 FROM items WHERE id='tw_paused'").fetchone() is None
    conn.close()


def test_ingest_twitter_reads_x_user_files_as_user_sources(tmp_path, monkeypatch):
    import ingest

    twitter_dir = tmp_path / "data" / "sources" / "twitter"
    twitter_dir.mkdir(parents=True)
    (twitter_dir / "x-user-karpathy.json").write_text(json.dumps({
        "ok": True,
        "handle": "karpathy",
        "data": [
            {
                "id": "tw1",
                "author": {"screenName": "karpathy", "name": "Andrej Karpathy"},
                "text": "hello",
                "metrics": {"likes": 3, "retweets": 1},
                "createdAt": "2026-07-05T00:00:00Z",
            }
        ],
    }))
    captured = []

    def fake_batch_upsert(conn, items):
        captured.extend(items)

    monkeypatch.setattr(ingest, "BASE", str(tmp_path))
    monkeypatch.setattr(ingest, "batch_upsert_current_run", fake_batch_upsert)
    monkeypatch.setattr(ingest, "_extract_twitter_posters_inline", lambda tasks: None)
    monkeypatch.setattr(ingest, "_run_asr_for_twitter_videos_inline", lambda conn, tweet_ids: None)

    count = ingest.ingest_twitter(conn=object(), timeline_only=False)

    assert count == 1
    assert captured[0]["platform"] == "twitter"
    assert captured[0]["source"] == "user:karpathy"
    assert captured[0]["url"] == "https://x.com/karpathy/status/tw1"
