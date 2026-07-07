"""订阅配置 v22.0 Wave 1: sources 注册表 schema + 种子导入单元测试。

不依赖网络/凭证/live fetch，全部用临时 SQLite。
"""
import os
import sys
import tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, os.path.join(BASE, "scripts"))


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


# ---- schema ----

def test_sources_table_columns(tmp_db):
    import db
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
    assert cols == {
        "id", "platform", "source_key", "display_name", "status",
        "config_json", "origin", "validated_at", "created_at", "updated_at",
        "consecutive_failures", "last_success_at", "last_error",
    }
    conn.close()


def test_items_has_source_id(tmp_db):
    import db
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
    assert "source_id" in cols
    conn.close()


def test_unique_platform_source_key(tmp_db):
    import db
    import sqlite3
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO sources(platform, source_key, status, origin) "
        "VALUES('rss','http://a/feed','active','seed_import')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sources(platform, source_key) VALUES('rss','http://a/feed')")
        conn.commit()
    conn.close()


# ---- seed import ----

def test_seed_inserts_config_sources(tmp_db):
    import db
    import seed_sources_registry as seed
    summary = seed.seed()
    conn = db.get_conn()
    # config-based named sources are seeded (rss/reddit/github/bilibili)
    assert summary["rss"]["inserted"] >= 1
    assert summary["reddit"]["inserted"] >= 1
    assert summary["github_repo"]["inserted"] >= 1
    assert summary["bilibili_up"]["inserted"] >= 1
    total = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    assert total == (summary["rss"]["inserted"] + summary["reddit"]["inserted"]
                     + summary["github_repo"]["inserted"]
                     + summary["bilibili_up"]["inserted"]
                     + summary.get("wechat_mp", {}).get("inserted", 0))
    conn.close()


def test_bilibili_seeded_not_fetched(tmp_db):
    import db
    import seed_sources_registry as seed
    seed.seed()
    conn = db.get_conn()
    statuses = {r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM sources WHERE platform='bilibili_up'").fetchall()}
    assert statuses == {"not_fetched"}
    # rss/reddit/github are active
    for plat in ("rss", "reddit", "github_repo"):
        st = {r[0] for r in conn.execute(
            "SELECT DISTINCT status FROM sources WHERE platform=?", (plat,)).fetchall()}
        assert st == {"active"}, f"{plat} expected active, got {st}"
    conn.close()


def test_seed_idempotent(tmp_db):
    import db
    import seed_sources_registry as seed
    seed.seed()
    conn = db.get_conn()
    total1 = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    seed.seed()
    total2 = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
    assert total1 == total2
    conn.close()


def test_seed_preserves_admin_status(tmp_db):
    import db
    import seed_sources_registry as seed
    seed.seed()
    conn = db.get_conn()
    key = conn.execute(
        "SELECT source_key FROM sources WHERE platform='rss' ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute("UPDATE sources SET status='paused' WHERE platform='rss' AND source_key=?",
                 (key,))
    conn.commit()
    seed.seed()  # re-run must NOT reset admin-changed status
    after = conn.execute(
        "SELECT status FROM sources WHERE platform='rss' AND source_key=?", (key,)
    ).fetchone()[0]
    assert after == "paused"
    conn.close()


# ---- ingest attribution + status filter (Wave 2a) ----

_FT = "2026-07-05T00:00:00Z"


def _item(item_id, platform, source):
    return {"id": item_id, "platform": platform, "source": source,
            "title": "t", "fetched_at": _FT}


def _seeded_conn():
    import db
    import seed_sources_registry as seed
    seed.seed()
    return db.get_conn()


def test_resolve_attaches_source_id_for_named_rss(tmp_db):
    import db
    conn = _seeded_conn()
    idx = db.load_source_index(conn)
    db.upsert_item(conn, _item("t1", "rss", "feed:simonwillison"), source_index=idx)
    sid = conn.execute("SELECT source_id FROM items WHERE id='t1'").fetchone()[0]
    assert sid is not None
    conn.close()


def test_algo_source_gets_null_source_id_but_inserts(tmp_db):
    import db
    conn = _seeded_conn()
    idx = db.load_source_index(conn)
    db.upsert_item(conn, _item("t2", "github", "trending:zh"), source_index=idx)
    row = conn.execute("SELECT source_id FROM items WHERE id='t2'").fetchone()
    assert row is not None and row[0] is None
    conn.close()


def test_paused_source_item_dropped(tmp_db):
    import db
    conn = _seeded_conn()
    conn.execute("UPDATE sources SET status='paused' WHERE platform='reddit' AND source_key='ClaudeAI'")
    conn.commit()
    idx = db.load_source_index(conn)
    ret = db.upsert_item(conn, _item("t3", "reddit", "r/ClaudeAI"), source_index=idx)
    assert ret == "dropped"
    assert conn.execute("SELECT 1 FROM items WHERE id='t3'").fetchone() is None
    conn.close()


def test_active_source_inserts_and_attaches(tmp_db):
    import db
    conn = _seeded_conn()
    idx = db.load_source_index(conn)
    ret = db.upsert_item(conn, _item("t4", "reddit", "r/OpenAI"), source_index=idx)
    row = conn.execute("SELECT source_id FROM items WHERE id='t4'").fetchone()
    assert ret != "dropped"
    assert row is not None and row[0] is not None
    conn.close()


def test_no_index_backward_compatible(tmp_db):
    import db
    conn = _seeded_conn()
    db.upsert_item(conn, _item("t5", "rss", "feed:gradient"))  # no source_index
    assert conn.execute("SELECT 1 FROM items WHERE id='t5'").fetchone() is not None
    conn.close()


def test_build_source_index_from_rows_accepts_dict_rows():
    import db
    idx = db.build_source_index_from_rows([
        {
            "id": 1,
            "platform": "rss",
            "source_key": "https://example.test/feed.xml",
            "status": "active",
            "config_json": '{"slug":"example"}',
        },
        {
            "id": 2,
            "platform": "reddit",
            "source_key": "ClaudeAI",
            "status": "paused",
            "config_json": None,
        },
        {
            "id": 3,
            "platform": "wechat_mp",
            "source_key": "lw-channel",
            "status": "active",
            "config_json": {"backend": "lingowhale"},
        },
        {
            "id": 4,
            "platform": "wechat_mp",
            "source_key": "https://wechat.example/feed",
            "status": "active",
            "config_json": "{}",
        },
        {
            "id": 5,
            "platform": "x_user",
            "source_key": "openai",
            "status": "broken",
            "config_json": "{}",
        },
        {
            "id": 6,
            "platform": "bilibili_up",
            "source_key": "12345",
            "status": "not_fetched",
            "config_json": "{}",
        },
    ])

    assert idx["rss_by_slug"]["example"] == (1, "active")
    assert idx["reddit_by_key"]["ClaudeAI"] == (2, "paused")
    assert idx["wechat_by_channel_id"]["lw-channel"] == (3, "active")
    assert idx["wechat_by_url"]["https://wechat.example/feed"] == (4, "active")
    assert idx["x_by_handle"]["openai"] == (5, "broken")
    assert idx["bili_by_uid"]["12345"] == (6, "not_fetched")


def test_normalize_active_source_row_parses_config_dict():
    import db
    assert db.normalize_active_source_row({
        "id": 9,
        "source_key": "openai",
        "display_name": "OpenAI",
        "config_json": '{"batch": 3}',
    }) == {
        "id": 9,
        "source_key": "openai",
        "display_name": "OpenAI",
        "config_json": {"batch": 3},
    }
    assert db.normalize_active_source_row({
        "id": 10,
        "source_key": "anthropic",
        "display_name": "Anthropic",
        "config_json": {"batch": 4},
    })["config_json"] == {"batch": 4}


def test_github_awesome_resolves_but_trending_does_not(tmp_db):
    import db
    conn = _seeded_conn()
    idx = db.load_source_index(conn)
    db.upsert_item(conn, _item("t6", "github", "awesome:modelcontextprotocol/registry"), source_index=idx)
    db.upsert_item(conn, _item("t7", "github", "trending:global"), source_index=idx)
    a = conn.execute("SELECT source_id FROM items WHERE id='t6'").fetchone()[0]
    b = conn.execute("SELECT source_id FROM items WHERE id='t7'").fetchone()[0]
    assert a is not None and b is None
    conn.close()


# ---- ingest chokepoint wiring (Wave 2b, forced local backend) ----

def test_ingest_chokepoint_attributes_and_drops(tmp_db, monkeypatch):
    monkeypatch.setenv("INFO2ACTION_FETCH_WRITE_BACKEND", "sqlite")
    import db
    import seed_sources_registry as seed
    seed.seed()
    import ingest
    ingest._source_index_loaded = False  # reset per-process cache
    conn = db.get_conn()

    # active rss attaches; algo github → None; both inserted via batch chokepoint
    ingest.batch_upsert_current_run(
        conn, [_item("b1", "rss", "feed:simonwillison"),
               _item("b2", "github", "trending:zh")])
    assert conn.execute("SELECT source_id FROM items WHERE id='b1'").fetchone()[0] is not None
    assert conn.execute("SELECT source_id FROM items WHERE id='b2'").fetchone()[0] is None

    # pause a reddit source → its item dropped by chokepoint
    conn.execute("UPDATE sources SET status='paused' WHERE platform='reddit' AND source_key='ClaudeAI'")
    conn.commit()
    ingest._source_index_loaded = False
    ingest.batch_upsert_current_run(
        conn, [_item("b3", "reddit", "r/ClaudeAI"), _item("b4", "reddit", "r/OpenAI")])
    assert conn.execute("SELECT 1 FROM items WHERE id='b3'").fetchone() is None  # paused dropped
    assert conn.execute("SELECT source_id FROM items WHERE id='b4'").fetchone()[0] is not None

    # single chokepoint also drops paused
    ret = ingest.upsert_item_current_backend(conn, _item("s1", "reddit", "r/ClaudeAI"))
    assert isinstance(ret, dict) and ret.get("dropped")
    assert conn.execute("SELECT 1 FROM items WHERE id='s1'").fetchone() is None
    conn.close()
