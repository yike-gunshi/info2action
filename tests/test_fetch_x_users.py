"""W2 subscription-config: fetch active X user handles and ingest as Twitter items."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
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


@pytest.fixture(autouse=True)
def default_legacy_tests_to_per_user_mode(monkeypatch):
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_X_FETCH_MODE", "per_user")
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {"fetch_mode": "per_user"},
    })


def test_x_fetch_mode_fails_safe_to_list_for_missing_or_unknown_config(monkeypatch):
    import fetch_x_users

    monkeypatch.delenv("INFO2ACTION_X_FETCH_MODE", raising=False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {})
    assert fetch_x_users._x_fetch_mode() == "list"

    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"fetch_mode": "typo"}})
    assert fetch_x_users._x_fetch_mode() == "list"


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
    _insert_source(conn, "x_user", "first_fetch", status="not_fetched")
    _insert_source(conn, "x_user", "paused_user", status="paused")

    handles = fetch_x_users._active_x_handles(conn)

    assert handles == ["karpathy", "sama", "first_fetch"]
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
        handle = args[2]
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
    assert calls[0] == ["twitter", "user-posts", "good", "-n", "7", "--json"]
    assert calls[1:] == [["twitter", "user-posts", "bad", "-n", "7", "--json"]]
    out_path = tmp_path / "sources" / "twitter" / "x-user-good.json"
    assert out_path.exists()
    payload = json.loads(out_path.read_text())
    assert payload["handle"] == "good"
    assert payload["data"][0]["author"]["screenName"] == "good"
    assert not (tmp_path / "sources" / "twitter" / "x-user-bad.json").exists()
    conn.close()


def test_fetch_x_users_preserves_full_tweet_media_fields(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    conn = db.get_conn()
    _insert_source(conn, "x_user", "openai")
    media = [
        {
            "type": "photo",
            "url": "https://pbs.twimg.com/media/example.jpg",
            "width": 1774,
            "height": 887,
        }
    ]
    urls = ["https://openai.com/example"]

    def fake_run(args, capture_output, text, timeout):
        assert args == ["twitter", "user-posts", "openai", "-n", "5", "--json"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([
                {
                    "id": "101",
                    "author": {"name": "OpenAI", "screenName": "OpenAI"},
                    "createdAt": "Fri Jul 11 00:00:00 +0000 2026",
                    "createdAtISO": "2026-07-11T00:00:00.000Z",
                    "isRetweet": False,
                    "lang": "en",
                    "media": media,
                    "metrics": {"likes": 10, "retweets": 2},
                    "text": "full tweet",
                    "urls": urls,
                }
            ]),
            stderr="",
        )

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)

    stats = fetch_x_users.fetch_x_users(conn, count=5)

    assert stats == {"handles": 1, "ok": 1, "failed": 0}
    payload = json.loads(
        (tmp_path / "sources" / "twitter" / "x-user-openai.json").read_text()
    )
    tweet = payload["data"][0]
    assert tweet["media"] == media
    assert tweet["urls"] == urls
    assert tweet["isRetweet"] is False
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


def test_fetch_x_users_remote_uses_remote_watermark_without_local_db(tmp_path, monkeypatch):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(
        remote_db,
        "list_active_sources_remote",
        lambda platform: [{"id": 201, "source_key": "karpathy"}],
    )
    monkeypatch.setattr(
        remote_db,
        "latest_x_user_watermark_remote",
        lambda source_id: "tw_100" if source_id == 201 else pytest.fail("wrong source"),
        raising=False,
    )
    monkeypatch.setattr(
        remote_db,
        "record_source_fetch_result_remote",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(db, "get_conn", lambda: pytest.fail("opened local db"))
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_user_posts",
        lambda handle, count: [
            {"id": "101", "text": "new"},
            {"id": "100", "text": "same"},
        ],
    )

    stats = fetch_x_users.fetch_x_users(count=2, batch_size=10)

    assert stats == {"handles": 1, "ok": 1, "failed": 0}
    payload = json.loads(
        (tmp_path / "sources" / "twitter" / "x-user-karpathy.json").read_text()
    )
    assert [tweet["id"] for tweet in payload["data"]] == ["101"]


def test_fetch_user_posts_classifies_rate_limit_for_wave_retry(monkeypatch):
    import fetch_x_users

    calls = []
    sleeps = []

    def fake_run(args, capture_output, text, timeout):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="", stderr="429 rate limit")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_x_users.time, "sleep", sleeps.append)

    with pytest.raises(fetch_x_users.XFetchError) as error:
        fetch_x_users._fetch_user_posts("karpathy", 10)

    assert error.value.code == "rate_limited"
    assert error.value.retryable is True
    assert len(calls) == 1
    assert sleeps == []


def test_fetch_user_posts_accepts_data_wrapper(monkeypatch):
    import fetch_x_users

    monkeypatch.setattr(
        fetch_x_users.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"data": [{"id": "101"}]}),
            stderr="",
        ),
    )

    assert fetch_x_users._fetch_user_posts("karpathy", 10) == [{"id": "101"}]


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
        handle = args[2]
        attempts[handle] = attempts.get(handle, 0) + 1
        if handle == "bad":
            return SimpleNamespace(returncode=1, stdout="", stderr="timeout")
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "101"}]), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_x_users.time, "sleep", lambda seconds: None)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 2, "ok": 1, "failed": 1}
    assert attempts == {"bad": 3, "good": 1}
    assert not (tmp_path / "sources" / "twitter" / "x-user-bad.json").exists()
    assert (tmp_path / "sources" / "twitter" / "x-user-good.json").exists()
    conn.close()


def test_fetch_x_users_attempts_every_registry_source_each_round(tmp_db, tmp_path, monkeypatch):
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
        handle = args[2]
        fetched.append(handle)
        return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": f"{handle}1"}]), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)

    first_stats = fetch_x_users.fetch_x_users(conn)
    second_stats = fetch_x_users.fetch_x_users(conn)

    assert first_stats == {"handles": 3, "ok": 3, "failed": 0}
    assert second_stats == {"handles": 3, "ok": 3, "failed": 0}
    assert sorted(fetched[:3]) == ["a", "b", "c"]
    assert sorted(fetched[3:]) == ["a", "b", "c"]
    cursor_path = tmp_path / "sources" / "twitter" / ".x_user_cursor.json"
    assert not cursor_path.exists()
    conn.close()


def test_fetch_x_users_finishes_first_wave_before_retrying(tmp_db, tmp_path, monkeypatch):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 3}})
    conn = db.get_conn()
    for handle in ("rate_limited", "second", "third"):
        _insert_source(conn, "x_user", handle)

    calls = []
    attempts = {}
    lock = threading.Lock()

    def fake_fetch(handle, count):
        with lock:
            attempts[handle] = attempts.get(handle, 0) + 1
            calls.append((handle, attempts[handle]))
        if handle == "rate_limited" and attempts[handle] == 1:
            raise fetch_x_users.XFetchError(
                "429 rate limit", code="rate_limited", retryable=True
            )
        return [{"id": f"{handle}-{attempts[handle]}"}]

    monkeypatch.setattr(fetch_x_users, "_fetch_user_posts", fake_fetch)
    monkeypatch.setattr(fetch_x_users.time, "sleep", lambda _seconds: None)

    stats = fetch_x_users.fetch_x_users(conn, workers=2, retry_rounds=1)

    assert stats == {"handles": 3, "ok": 3, "failed": 0}
    retry_index = calls.index(("rate_limited", 2))
    assert retry_index > calls.index(("second", 1))
    assert retry_index > calls.index(("third", 1))
    conn.close()


def test_fetch_x_users_still_fetches_when_watermark_lookup_fails(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    conn = db.get_conn()
    _insert_source(conn, "x_user", "karpathy")
    fetched = []
    monkeypatch.setattr(
        fetch_x_users,
        "_latest_x_user_watermark",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("watermark unavailable")),
    )
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_user_posts",
        lambda handle, _count: fetched.append(handle) or [{"id": "101"}],
    )

    stats = fetch_x_users.fetch_x_users(conn, workers=1, retry_rounds=0)

    assert stats == {"handles": 1, "ok": 1, "failed": 0}
    assert fetched == ["karpathy"]
    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["attempted"] == 1
    assert summary["missed"] == 0
    conn.close()


def test_fetch_x_users_bounds_concurrency_and_persists_attempt_summary(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fetch_x_users, "CONFIG", {"twitter": {"user_posts_count": 3}})
    conn = db.get_conn()
    source_ids = [
        _insert_source(conn, "x_user", handle)
        for handle in ("a", "b", "c", "d", "e", "f")
    ]

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_fetch(handle, count):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return [{"id": f"{handle}-1"}]

    monkeypatch.setattr(fetch_x_users, "_fetch_user_posts", fake_fetch)

    stats = fetch_x_users.fetch_x_users(conn, workers=2, retry_rounds=0)

    assert stats == {"handles": 6, "ok": 6, "failed": 0}
    assert max_active == 2
    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["planned_source_ids"] == source_ids
    assert summary["planned"] == 6
    assert summary["attempted"] == 6
    assert summary["succeeded"] == 6
    assert summary["failed"] == 0
    assert summary["missed"] == 0
    assert {result["outcome"] for result in summary["results"]} == {"success"}
    conn.close()


def test_fetch_x_users_persists_completed_result_before_wave_finishes(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    conn = db.get_conn()
    _insert_source(conn, "x_user", "fast")
    _insert_source(conn, "x_user", "slow")
    conn.close()

    fast_finished = threading.Event()
    release_slow = threading.Event()
    completed = {}

    def fake_fetch(handle, count):
        if handle == "slow":
            assert release_slow.wait(timeout=5)
        else:
            fast_finished.set()
        return [{"id": f"{handle}-1"}]

    monkeypatch.setattr(fetch_x_users, "_fetch_user_posts", fake_fetch)

    thread = threading.Thread(
        target=lambda: completed.update(
            stats=fetch_x_users.fetch_x_users(workers=2, retry_rounds=0)
        ),
        daemon=True,
    )
    thread.start()
    try:
        assert fast_finished.wait(timeout=2)
        summary_path = tmp_path / "x_user_attempts.json"
        deadline = time.monotonic() + 1
        summary = {}
        while time.monotonic() < deadline:
            summary = json.loads(summary_path.read_text())
            if summary["attempted"] == 1:
                break
            time.sleep(0.01)

        assert summary["attempted"] == 1
        assert summary["missed"] == 1
        assert [result["handle"] for result in summary["results"]] == ["fast"]
    finally:
        release_slow.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert completed["stats"] == {"handles": 2, "ok": 2, "failed": 0}


def test_fetch_x_users_list_mode_maps_one_timeline_to_every_registry_source(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFO2ACTION_X_FETCH_MODE", "list")
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {"fetch_mode": "list", "x_list_id": "123456", "list_fetch_count": 60},
    })
    conn = db.get_conn()
    source_ids = {
        handle: _insert_source(conn, "x_user", handle)
        for handle in ("Alpha", "beta", "quiet")
    }
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_user_posts",
        lambda *_args, **_kwargs: pytest.fail("list mode called per-user fetch"),
    )
    monkeypatch.setattr(
        fetch_x_users,
        "_ensure_x_list_members",
        lambda sources: {
            "configured": True,
            "list_id": "123456",
            "synced_handles": [source["source_key"] for source in sources],
            "pending_handles": [],
            "failed": [],
        },
        raising=False,
    )
    calls = []

    def fake_fetch_list(list_id, count):
        calls.append((list_id, count))
        return [
            {"id": "301", "author": {"screenName": "alpha"}, "text": "a"},
            {"id": "302", "author": {"screenName": "BETA"}, "text": "b"},
            {"id": "999", "author": {"screenName": "not_configured"}, "text": "x"},
        ]

    monkeypatch.setattr(fetch_x_users, "_fetch_list_posts", fake_fetch_list, raising=False)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 3, "ok": 3, "failed": 0}
    assert calls == [("123456", 60)]
    assert json.loads((tmp_path / "sources/twitter/x-user-Alpha.json").read_text())["data"][0]["id"] == "301"
    assert json.loads((tmp_path / "sources/twitter/x-user-beta.json").read_text())["data"][0]["id"] == "302"
    assert json.loads((tmp_path / "sources/twitter/x-user-quiet.json").read_text())["data"] == []

    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["mode"] == "list"
    assert summary["list_id"] == "123456"
    assert summary["planned_source_ids"] == list(source_ids.values())
    assert summary["attempted"] == 3
    assert summary["succeeded"] == 3
    assert summary["no_new"] == 1
    assert summary["failed"] == 0
    assert summary["unmatched_posts"] == 1
    conn.close()


def test_fetch_x_users_list_failure_records_one_real_failure_for_every_source(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFO2ACTION_X_FETCH_MODE", "list")
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {"fetch_mode": "list", "x_list_id": "123456", "list_fetch_count": 500},
    })
    conn = db.get_conn()
    source_ids = [
        _insert_source(conn, "x_user", handle)
        for handle in ("alpha", "beta")
    ]
    monkeypatch.setattr(
        fetch_x_users,
        "_ensure_x_list_members",
        lambda sources: {
            "configured": True,
            "list_id": "123456",
            "synced_handles": [source["source_key"] for source in sources],
            "pending_handles": [],
            "failed": [],
        },
        raising=False,
    )
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_list_posts",
        lambda *_args: (_ for _ in ()).throw(
            fetch_x_users.XFetchError("HTTP 429", code="rate_limited", retryable=True)
        ),
        raising=False,
    )

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 2, "ok": 0, "failed": 2}
    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["mode"] == "list"
    assert summary["planned_source_ids"] == source_ids
    assert summary["attempted"] == 2
    assert summary["failed"] == 2
    assert summary["missed"] == 0
    assert {row["error_code"] for row in summary["results"]} == {"rate_limited"}
    assert not (tmp_path / "sources/twitter/x-user-alpha.json").exists()
    assert not (tmp_path / "sources/twitter/x-user-beta.json").exists()
    conn.close()


def test_list_pending_members_use_grouped_search_fallback(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFO2ACTION_X_FETCH_MODE", "list")
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {
            "fetch_mode": "list",
            "x_list_id": "123456",
            "list_fetch_count": 500,
            "pending_search_batch_size": 8,
            "pending_search_workers": 3,
        },
    })
    conn = db.get_conn()
    _insert_source(conn, "x_user", "alpha")
    _insert_source(conn, "x_user", "beta")
    monkeypatch.setattr(
        fetch_x_users,
        "_ensure_x_list_members",
        lambda sources: {
            "configured": True,
            "list_id": "123456",
            "synced_handles": ["alpha"],
            "pending_handles": ["beta"],
            "failed": [{"handle": "beta", "error": "member write limited"}],
        },
    )
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_list_posts",
        lambda *_args: [{"id": "401", "author": {"screenName": "alpha"}, "text": "a"}],
    )
    search_calls = []

    def fake_search(handles, count):
        search_calls.append((handles, count))
        return [{"id": "402", "author": {"screenName": "BETA"}, "text": "b"}]

    monkeypatch.setattr(fetch_x_users, "_fetch_search_posts", fake_search, raising=False)

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 2, "ok": 2, "failed": 0}
    assert search_calls == [(["beta"], 500)]
    assert json.loads((tmp_path / "sources/twitter/x-user-alpha.json").read_text())["data"][0]["id"] == "401"
    assert json.loads((tmp_path / "sources/twitter/x-user-beta.json").read_text())["data"][0]["id"] == "402"
    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["succeeded"] == 2
    assert summary["failed"] == 0
    assert summary["fallback_sources"] == 1
    conn.close()


def test_multi_list_mode_fetches_all_configured_lists_and_falls_back_for_pending(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import fetch_x_users
    import remote_db

    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INFO2ACTION_X_FETCH_MODE", "list")
    monkeypatch.delenv("INFO2ACTION_X_LIST_ID", raising=False)
    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(fetch_x_users, "CONFIG", {
        "twitter": {
            "fetch_mode": "list",
            "x_lists": [
                {"key": "official", "name": "Official", "list_id": "111"},
                {"key": "people", "name": "People", "list_id": "222"},
            ],
            "list_fetch_count": 500,
        },
    })
    conn = db.get_conn()
    _insert_source(conn, "x_user", "openai", config={"x_list_key": "official"})
    _insert_source(conn, "x_user", "karpathy", config={"x_list_key": "people"})
    _insert_source(conn, "x_user", "pending", config={"x_list_key": "people"})
    monkeypatch.setattr(
        fetch_x_users,
        "_ensure_x_list_members",
        lambda sources: {
            "configured": True,
            "list_id": None,
            "lists": [
                {"key": "official", "list_id": "111", "synced_handles": ["openai"]},
                {"key": "people", "list_id": "222", "synced_handles": ["karpathy"]},
            ],
            "synced_handles": ["openai", "karpathy"],
            "pending_handles": ["pending"],
            "failed": [],
        },
    )
    list_calls = []

    def fake_list(list_id, count):
        list_calls.append((list_id, count))
        handle = "openai" if list_id == "111" else "karpathy"
        return [{"id": f"{list_id}1", "author": {"screenName": handle}, "text": handle}]

    monkeypatch.setattr(fetch_x_users, "_fetch_list_posts", fake_list)
    monkeypatch.setattr(
        fetch_x_users,
        "_fetch_search_posts",
        lambda handles, count: [
            {"id": "3331", "author": {"screenName": "pending"}, "text": "pending"}
        ],
    )

    stats = fetch_x_users.fetch_x_users(conn)

    assert stats == {"handles": 3, "ok": 3, "failed": 0}
    assert sorted(list_calls) == [("111", 500), ("222", 500)]
    summary = json.loads((tmp_path / "x_user_attempts.json").read_text())
    assert summary["list_ids"] == ["111", "222"]
    assert summary["fallback_sources"] == 1
    assert summary["attempted"] == 3
    conn.close()


def test_fetch_search_posts_groups_configured_handles_without_personal_flow(monkeypatch):
    import fetch_x_users

    calls = []

    def fake_run(args, capture_output, text, timeout):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": []}), stderr="")

    monkeypatch.setattr(fetch_x_users.subprocess, "run", fake_run)

    assert fetch_x_users._fetch_search_posts(["alpha", "beta"], 500) == []
    assert calls == [[
        "twitter", "search", "(from:alpha OR from:beta)",
        "-t", "latest", "-n", "500", "--json",
    ]]
    assert all(token not in calls[0] for token in ("following", "for-you", "bookmarks", "user-posts"))


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
    monkeypatch.setattr(ingest, "_source_index_for", lambda conn: {
        "x_by_handle": {"karpathy": (1, "active")},
    })
    monkeypatch.setattr(ingest, "batch_upsert_current_run", fake_batch_upsert)
    monkeypatch.setattr(ingest, "_extract_twitter_posters_inline", lambda tasks: None)
    monkeypatch.setattr(ingest, "_run_asr_for_twitter_videos_inline", lambda conn, tweet_ids: None)

    count = ingest.ingest_twitter(conn=object(), timeline_only=False)

    assert count == 1
    assert captured[0]["platform"] == "twitter"
    assert captured[0]["source"] == "user:karpathy"
    assert captured[0]["url"] == "https://x.com/karpathy/status/tw1"
