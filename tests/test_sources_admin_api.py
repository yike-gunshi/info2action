"""订阅配置 Wave 3: admin-only sources API tests."""
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
import pytest
from fastapi.testclient import TestClient

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db as db_mod  # noqa: E402

PASSWORD = "password123"


def _hash_password(password):
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt(rounds=4)).decode()


def _create_user(conn, username, email, role):
    user_id = str(uuid.uuid4())
    db_mod.create_user(conn, user_id, username, email, _hash_password(PASSWORD), role=role)
    db_mod.update_user(conn, user_id, email_verified=1)
    return user_id


@pytest.fixture
def sources_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", "sources-admin-test-secret")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")
    monkeypatch.setenv("INFO2ACTION_DATA_AUTHORITY", "local")
    monkeypatch.setenv("INFO2ACTION_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_FEED_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_EVENT_READ_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_STATUS_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "sqlite")
    monkeypatch.setenv("INFO2ACTION_STORAGE_MODE", "local")
    monkeypatch.setenv("INFO2ACTION_ASSET_BACKEND", "local")
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    db_mod._item_status_has_user_id = None

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "twitter": {"following_count": 50, "for_you_count": 50},
        "bilibili": {"hot_count": 10, "rank_count": 10, "videos_per_up": 3},
        "hackernews": {"count": 30},
        "github_trending": {"count": 25, "since": "daily", "spoken_languages": ["zh", ""]},
    }), encoding="utf-8")

    conn = db_mod.get_conn()
    try:
        _create_user(conn, "admin-sources", "admin-sources@test.local", "admin")
        _create_user(conn, "user-sources", "user-sources@test.local", "user")
    finally:
        conn.close()

    import app as app_mod
    import middleware.auth as auth_mw
    import routes.auth as auth_route
    import routes.sources as sources_route

    monkeypatch.setattr(auth_route, "JWT_SECRET", "sources-admin-test-secret")
    monkeypatch.setattr(auth_mw, "_AUTH_TOKEN", "")
    monkeypatch.setattr(sources_route, "BASE", str(tmp_path))
    app_mod.app.state.limiter.enabled = False
    return {"app": app_mod.app, "base": tmp_path}


def _login(app, email):
    client = TestClient(app)
    resp = client.post("/api/auth/login", json={"login": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return client


def _insert_source(conn, platform, source_key, **overrides):
    data = {
        "display_name": overrides.get("display_name", source_key),
        "status": overrides.get("status", "active"),
        "config_json": json.dumps(overrides["config_json"]) if "config_json" in overrides else None,
        "origin": overrides.get("origin", "seed_import"),
    }
    cur = conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, config_json, origin)
           VALUES(?,?,?,?,?,?)""",
        (platform, source_key, data["display_name"], data["status"],
         data["config_json"], data["origin"]),
    )
    conn.commit()
    return cur.lastrowid


def _set_x_user_gray_limit(base, limit):
    config_path = base / "config" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("twitter", {})["x_user_gray_limit"] = limit
    config_path.write_text(json.dumps(config), encoding="utf-8")


def test_list_sources_groups_non_deleted_sources_and_health(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    fetched_at = datetime.now(timezone.utc) - timedelta(days=1)
    started_at = fetched_at - timedelta(minutes=5)
    last_success_at = fetched_at - timedelta(minutes=1)
    fetched_at_text = fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    started_at_text = started_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    last_success_at_text = last_success_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db_mod.get_conn()
    try:
        rss_id = _insert_source(
            conn,
            "rss",
            "https://example.com/feed.xml",
            display_name="Example Feed",
            config_json={"slug": "example"},
        )
        _insert_source(conn, "reddit", "OpenAI", display_name="OpenAI")
        _insert_source(conn, "rss", "https://deleted.test/feed.xml", status="deleted")
        run_id = conn.execute(
            """INSERT INTO fetch_runs(started_at, finished_at, status)
               VALUES(?, ?, 'success')""",
            (started_at_text, fetched_at_text),
        ).lastrowid
        conn.execute(
            """INSERT INTO items(id, platform, source, source_id, title, fetched_at)
               VALUES('item-rss', 'rss', 'feed:example', ?, 'Title', ?)""",
            (rss_id, fetched_at_text),
        )
        conn.execute(
            """INSERT INTO fetch_run_items(run_id, item_id, platform, source, was_inserted)
               VALUES(?, 'item-rss', 'rss', 'feed:example', 1)""",
            (run_id,),
        )
        conn.execute(
            """UPDATE sources
                   SET consecutive_failures = 2,
                       last_success_at = ?,
                       last_error = 'temporary timeout'
                   WHERE id = ?""",
            (last_success_at_text, rss_id),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin.get("/api/admin/sources")
    assert resp.status_code == 200, resp.text
    groups = {g["platform"]: g["sources"] for g in resp.json()["groups"]}
    assert set(groups) == {"reddit", "rss"}
    rss = groups["rss"][0]
    reddit = groups["reddit"][0]
    assert rss["source_key"] == "https://example.com/feed.xml"
    assert rss["consecutive_failures"] == 2
    assert rss["last_success_at"] == last_success_at_text
    assert rss["last_error"] == "temporary timeout"
    assert rss["health"]["last_fetched_at"] == fetched_at_text
    assert rss["health"]["inserted_7d"] == 1
    assert rss["health"]["consecutive_failures"] == 2
    assert reddit["health"]["last_fetched_at"] is None
    assert reddit["health"]["inserted_7d"] is None


def test_list_sources_marks_successful_zero_output_as_fetched(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    conn = db_mod.get_conn()
    try:
        source_id = _insert_source(
            conn,
            "rss",
            "https://zero-output.test/feed.xml",
            display_name="Zero Output",
            config_json={"slug": "zero-output"},
        )
        conn.execute(
            "UPDATE sources SET last_success_at = ? WHERE id = ?",
            ("2026-07-10T01:02:03Z", source_id),
        )
        conn.commit()
    finally:
        conn.close()

    resp = admin.get("/api/admin/sources")

    assert resp.status_code == 200, resp.text
    sources = {
        source["source_key"]: source
        for group in resp.json()["groups"]
        for source in group["sources"]
    }
    health = sources["https://zero-output.test/feed.xml"]["health"]
    assert health["last_fetched_at"] == "2026-07-10T01:02:03Z"
    assert health["inserted_7d"] == 0


def test_list_sources_includes_latest_x_run_coverage_and_attempts(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    conn = db_mod.get_conn()
    try:
        success_id = _insert_source(conn, "x_user", "success")
        failed_id = _insert_source(conn, "x_user", "failed")
        missed_id = _insert_source(conn, "x_user", "missed", status="not_fetched")
        summary = {
            "started_at": "2026-07-11T01:00:00+00:00",
            "finished_at": "2026-07-11T01:10:00+00:00",
            "planned": 3,
            "attempted": 2,
            "succeeded": 1,
            "no_new": 0,
            "failed": 1,
            "missed": 1,
            "missed_source_ids": [missed_id],
            "results": [
                {
                    "source_id": success_id,
                    "handle": "success",
                    "outcome": "success",
                    "attempts": 1,
                    "new_count": 2,
                },
                {
                    "source_id": failed_id,
                    "handle": "failed",
                    "outcome": "failed",
                    "attempts": 3,
                    "error_code": "rate_limited",
                    "error": "429 rate limit",
                },
            ],
        }
        conn.execute(
            """INSERT INTO fetch_runs(started_at, finished_at, status, stats_json)
               VALUES(?, ?, 'done', ?)""",
            (
                summary["started_at"],
                summary["finished_at"],
                json.dumps({"_x_source_attempts": summary}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    body = admin.get("/api/admin/sources").json()

    assert body["latest_x_run"] == {
        "run_id": 1,
        "started_at": "2026-07-11T01:00:00+00:00",
        "finished_at": "2026-07-11T01:10:00+00:00",
        "planned": 3,
        "attempted": 2,
        "succeeded": 1,
        "no_new": 0,
        "failed": 1,
        "missed": 1,
    }
    x_sources = {
        source["source_key"]: source
        for group in body["groups"] if group["platform"] == "x_user"
        for source in group["sources"]
    }
    assert x_sources["success"]["health"]["latest_attempt"]["outcome"] == "success"
    assert x_sources["failed"]["health"]["latest_attempt"]["error_code"] == "rate_limited"
    assert x_sources["missed"]["health"]["latest_attempt"]["outcome"] == "missed"


@pytest.mark.parametrize("payload", [
    {"platform": "x_user", "source_key": "bad;handle"},
    {"platform": "reddit", "source_key": "bad sub"},
    {"platform": "github_repo", "source_key": "owner/repo;bad"},
    {"platform": "rss", "source_key": "javascript:alert(1)"},
    {"platform": "wechat_mp", "source_key": "bad channel!"},
])
def test_validate_rejects_source_key_outside_whitelist(sources_env, payload):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    resp = admin.post("/api/admin/sources/validate", json=payload)
    assert resp.status_code == 400
    assert "source_key" in resp.json()["error"]


def test_validate_wechat_mp_url_uses_rss_validator(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    calls = []

    def fake_validate_rss(source_key):
        calls.append(source_key)
        return {
            "status": "ok",
            "platform": "rss",
            "source_key": source_key,
            "preview": [{"title": "Hello"}],
        }

    monkeypatch.setattr(sources_route, "_validate_rss", fake_validate_rss)

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "wechat_mp",
        "source_key": "https://wechat.example.com/feed.xml",
    })

    assert resp.status_code == 200, resp.text
    assert calls == ["https://wechat.example.com/feed.xml"]
    body = resp.json()
    assert body["platform"] == "wechat_mp"
    assert body["source_key"] == "https://wechat.example.com/feed.xml"


def test_validate_wechat_mp_channel_id_uses_lingowhale_backend(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    monkeypatch.setattr(
        sources_route,
        "_validate_rss",
        lambda source_key: pytest.fail("channel_id validation should not call RSS"),
    )

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "wechat_mp",
        "source_key": "lw-channel_123",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "ok",
        "platform": "wechat_mp",
        "source_key": "lw-channel_123",
        "backend": "lingowhale",
        "preview": [],
    }


def test_validate_x_user_uses_twitter_cli_bare_tweet_list(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    calls = []

    def fake_run(args, capture_output, text, timeout):
        calls.append(args)
        assert capture_output is True
        assert text is True
        assert timeout == 30
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps([
                {
                    "id": "123",
                    "text": "Hello from X",
                    "time": "2026-07-07T10:00:00Z",
                    "author": {"screenName": "@openai"},
                }
            ]),
            stderr="",
        )

    monkeypatch.setattr(sources_route.subprocess, "run", fake_run)

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "x_user",
        "source_key": "openai",
    })

    assert resp.status_code == 200, resp.text
    assert calls == [["twitter", "--compact", "user-posts", "openai", "-n", "3", "--json"]]
    body = resp.json()
    assert body["status"] == "ok"
    assert body["display_name"] == "openai"
    assert body["preview"] == [{
        "title": "Hello from X",
        "url": "https://x.com/openai/status/123",
        "published_at": "2026-07-07T10:00:00Z",
        "summary": "Hello from X",
    }]


def test_validate_x_user_empty_timeline_returns_empty(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    def fake_run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="[]",
            stderr="",
        )

    monkeypatch.setattr(sources_route.subprocess, "run", fake_run)

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "x_user",
        "source_key": "openai",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "empty"
    assert resp.json()["preview"] == []


def test_validate_x_user_missing_twitter_cli_returns_deferred(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    def fake_run(args, capture_output, text, timeout):
        raise FileNotFoundError("twitter")

    monkeypatch.setattr(sources_route.subprocess, "run", fake_run)

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "x_user",
        "source_key": "openai",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deferred"
    assert body["reason"] == "X validation requires the local twitter CLI session, which is unavailable in this environment."


def test_validate_x_user_timeout_returns_empty_warning(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    def fake_run(args, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

    monkeypatch.setattr(sources_route.subprocess, "run", fake_run)

    resp = admin.post("/api/admin/sources/validate", json={
        "platform": "x_user",
        "source_key": "openai",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "empty"
    assert body["preview"] == []
    assert body["warning"] == "校验超时，可先入库"


def test_create_wechat_mp_channel_id_sets_lingowhale_backend(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    resp = admin.post("/api/admin/sources", json={
        "platform": "wechat_mp",
        "source_key": "lw-channel_123",
        "display_name": "语鲸公众号",
    })

    assert resp.status_code == 200, resp.text
    source = resp.json()["source"]
    assert source["source_key"] == "lw-channel_123"
    assert source["config_json"]["backend"] == "lingowhale"


def test_create_wechat_mp_url_sets_rss_backend(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    resp = admin.post("/api/admin/sources", json={
        "platform": "wechat_mp",
        "source_key": "https://wechat.example.com/feed.xml",
        "display_name": "RSS 公众号",
    })

    assert resp.status_code == 200, resp.text
    source = resp.json()["source"]
    assert source["source_key"] == "https://wechat.example.com/feed.xml"
    assert source["config_json"]["backend"] == "rss"


def test_search_wechat_marks_existing_registry_sources(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import remote_db
    import routes.sources as sources_route

    conn = db_mod.get_conn()
    try:
        _insert_source(conn, "wechat_mp", "ch-active", display_name="已添加")
        _insert_source(conn, "wechat_mp", "ch-deleted", status="deleted", display_name="已删除")
    finally:
        conn.close()

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(sources_route.remote_db, "fetch_write_to_remote", lambda: False)
    monkeypatch.setattr(
        sources_route.fetch_lingowhale,
        "search_channels",
        lambda q, limit=20: [
            {"channel_id": "ch-active", "name": "已添加"},
            {"channel_id": "ch-deleted", "name": "已删除"},
            {"channel_id": "ch-new", "name": "新号"},
        ],
    )

    resp = admin.get("/api/admin/sources/search-wechat?q=赛博&limit=3")

    assert resp.status_code == 200, resp.text
    channels = resp.json()["channels"]
    assert [ch["channel_id"] for ch in channels] == ["ch-active", "ch-deleted", "ch-new"]
    assert [ch["already_in_registry"] for ch in channels] == [True, False, False]


def test_search_wechat_upstream_error_returns_502(sources_env, monkeypatch):
    admin = _login(sources_env["app"], "admin-sources@test.local")

    import routes.sources as sources_route

    def boom(q, limit=20):
        raise RuntimeError("lingowhale search failed: code=10010 msg=token expired")

    monkeypatch.setattr(sources_route.fetch_lingowhale, "search_channels", boom)

    resp = admin.get("/api/admin/sources/search-wechat?q=赛博")

    assert resp.status_code == 502
    assert "lingowhale search failed" in resp.json()["error"]


def test_create_soft_delete_and_revive_preserves_source_id(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    payload = {
        "platform": "rss",
        "source_key": "https://example.com/feed.xml",
        "display_name": "Example Feed",
    }

    created = admin.post("/api/admin/sources", json=payload)
    assert created.status_code == 200, created.text
    source_id = created.json()["source"]["id"]

    deleted = admin.delete(f"/api/admin/sources/{source_id}")
    assert deleted.status_code == 200, deleted.text
    assert admin.get("/api/admin/sources").json()["total"] == 0

    revived = admin.post("/api/admin/sources", json=payload)
    assert revived.status_code == 200, revived.text
    assert revived.json()["source"]["id"] == source_id
    assert revived.json()["source"]["status"] == "active"

    conn = db_mod.get_conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM sources WHERE platform='rss' AND source_key=?",
            (payload["source_key"],),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_create_x_user_ignores_retired_gray_limit(sources_env):
    _set_x_user_gray_limit(sources_env["base"], 2)
    admin = _login(sources_env["app"], "admin-sources@test.local")
    conn = db_mod.get_conn()
    try:
        _insert_source(conn, "x_user", "alpha_ai")
        _insert_source(conn, "x_user", "beta_ai")
    finally:
        conn.close()

    resp = admin.post("/api/admin/sources", json={
        "platform": "x_user",
        "source_key": "gamma_ai",
        "display_name": "Gamma",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "gray_gated" not in body
    assert body["source"]["status"] == "active"


def test_patch_x_user_active_ignores_retired_gray_limit(sources_env):
    _set_x_user_gray_limit(sources_env["base"], 2)
    admin = _login(sources_env["app"], "admin-sources@test.local")
    conn = db_mod.get_conn()
    try:
        _insert_source(conn, "x_user", "alpha_ai")
        _insert_source(conn, "x_user", "beta_ai")
        source_id = _insert_source(conn, "x_user", "gamma_ai", status="paused")
    finally:
        conn.close()

    resp = admin.patch(f"/api/admin/sources/{source_id}", json={"status": "active"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "gray_gated" not in body
    assert body["source"]["status"] == "active"

    conn = db_mod.get_conn()
    try:
        row = conn.execute("SELECT status FROM sources WHERE id = ?", (source_id,)).fetchone()
        assert row["status"] == "active"
    finally:
        conn.close()


def test_create_rss_ignores_x_user_gray_limit(sources_env):
    _set_x_user_gray_limit(sources_env["base"], 0)
    admin = _login(sources_env["app"], "admin-sources@test.local")

    resp = admin.post("/api/admin/sources", json={
        "platform": "rss",
        "source_key": "https://example.com/feed.xml",
        "display_name": "Example Feed",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["source"]["status"] == "active"


def test_patch_pause_resume_and_config_json(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    created = admin.post("/api/admin/sources", json={
        "platform": "reddit",
        "source_key": "OpenAI",
        "display_name": "OpenAI",
    })
    source_id = created.json()["source"]["id"]

    paused = admin.patch(f"/api/admin/sources/{source_id}", json={
        "status": "paused",
        "config_json": {"limit": 12},
    })
    assert paused.status_code == 200, paused.text
    assert paused.json()["source"]["status"] == "paused"
    assert paused.json()["source"]["config_json"] == {"limit": 12}

    resumed = admin.patch(f"/api/admin/sources/{source_id}", json={"status": "active"})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["source"]["status"] == "active"


def test_lingowhale_reconcile_returns_empty_when_snapshot_missing(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    resp = admin.post("/api/admin/sources/lingowhale/reconcile", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["missing"] == []
    assert body["imported"] == []
    assert body["note"]


def test_algo_params_read_write_config_json(sources_env):
    admin = _login(sources_env["app"], "admin-sources@test.local")
    before = admin.get("/api/admin/sources/algo-params")
    assert before.status_code == 200, before.text
    assert before.json()["params"]["hackernews_count"] == 30
    assert before.json()["params"]["github_trending_count"] == 25
    assert "twitter_following_count" not in before.json()["params"]
    assert "twitter_for_you_count" not in before.json()["params"]

    patch = admin.patch("/api/admin/sources/algo-params", json={
        "hackernews_count": 31,
        "github_trending_count": 26,
        "bilibili_hot_count": 12,
        "bilibili_rank_count": 13,
    })
    assert patch.status_code == 200, patch.text
    assert patch.json()["params"]["hackernews_count"] == 31
    assert patch.json()["params"]["github_trending_count"] == 26

    saved = json.loads((sources_env["base"] / "config" / "config.json").read_text(encoding="utf-8"))
    assert saved["hackernews"]["count"] == 31
    assert saved["github_trending"]["count"] == 26
    assert saved["bilibili"]["hot_count"] == 12
    assert saved["bilibili"]["rank_count"] == 13


def test_sources_api_rejects_non_admin_user(sources_env):
    regular = _login(sources_env["app"], "user-sources@test.local")
    resp = regular.get("/api/admin/sources")
    assert resp.status_code == 403
