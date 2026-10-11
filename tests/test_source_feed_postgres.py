"""Opt-in source-feed SQL regression tests against an isolated local PostgreSQL.

Run with INFO2ACTION_TEST_POSTGRES_URL pointing at a loopback test server.
Every test creates and removes its own UUID-named schema; these
tests reject remote hosts and never use the application's production DB URL.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import remote_db


@pytest.fixture
def source_postgres(monkeypatch):
    url = os.environ.get("INFO2ACTION_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("INFO2ACTION_TEST_POSTGRES_URL is not set")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    connection_info = conninfo_to_dict(url)
    loopback = {"127.0.0.1", "localhost", "::1"}
    if connection_info.get("host") not in loopback or (
        connection_info.get("hostaddr")
        and connection_info["hostaddr"] not in loopback
    ):
        pytest.fail("PostgreSQL regression tests require an explicit loopback host")
    schema = "source_feed_test_" + uuid4().hex

    @contextmanager
    def connect():
        with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    monkeypatch.setattr(remote_db, "connect", connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: schema)
    monkeypatch.setattr(remote_db, "remote_authority_enabled", lambda: True)
    monkeypatch.setattr(remote_db, "_source_display_min_stars", lambda: 50)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *a, **k: None)
    monkeypatch.setattr(remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL", 0.0)
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_ENV, "0")
    remote_db._cache_clear_all()

    text_columns = (
        "id user_id platform source title content author_name author_id "
        "author_avatar url cover_url lang description ai_summary ai_key_points "
        "ai_category ai_keywords multi_l1_reason content_type"
    ).split()
    json_columns = (
        "media_json metrics_json tags_json detail_json comments_json "
        "ai_categories ai_subcategories ai_extracted"
    ).split()
    timestamp_columns = "fetched_at published_at created_at".split()
    columns = (
        [f"{name} text" for name in text_columns]
        + [f"{name} jsonb" for name in json_columns]
        + [f"{name} timestamptz" for name in timestamp_columns]
        + ["visible integer", "relevance_score double precision", "PRIMARY KEY(id)"]
    )
    now = datetime.now(timezone.utc).replace(microsecond=123456)
    platforms = ("twitter", "lingowhale", "waytoagi", "github", "reddit", "rss", "hackernews")
    rows = []
    for platform in platforms:
        for index in range(140):
            fetched = now - timedelta(days=index // 2)
            rows.append((
                f"{platform}-{index:04d}", None, platform,
                "following" if index % 2 else "channel-a", f"{platform} item {index}",
                1, None if index % 5 == 0 else float(index % 7), fetched,
                None if index % 4 == 0 else fetched,
                Jsonb(["products", "products", "coding"] if index % 3 == 0 else ["coding"]),
                "products" if index % 3 == 0 else "coding", Jsonb({"stars": 100}),
            ))
    for owner in ("owner-a", "owner-b"):
        for index in range(15):
            rows.append((
                f"manual-{owner}-{index:03d}", owner, "manual", "manual", owner,
                1, float(index % 3), now - timedelta(days=index), now - timedelta(days=index),
                Jsonb(["products"]), "products", Jsonb({}),
            ))
    for item_id, source, visible, fetched, owner in (
        ("hidden", "following", 0, now, None),
        ("bookmark", "bookmarks", 1, now, None),
        ("search", "search:q", 1, now, None),
        ("old", "following", 1, now - timedelta(days=91), None),
        ("stranger", "following", 1, now, "owner-b"),
    ):
        rows.append((item_id, owner, "twitter", source, item_id, visible, 1.0,
                     fetched, now, Jsonb(["products"]), "products", Jsonb({})))
    rows.append(("legacy-null-array", None, "twitter", "following", "legacy", 1, 1.0,
                 now, now, None, "products", Jsonb({})))

    with connect() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.items ({','.join(columns)})")
        conn.execute(f"CREATE TABLE {schema}.item_status (item_id text, user_id text, "
                     "read_at timestamptz, clicked_at timestamptz, starred_at timestamptz, "
                     "hidden_at timestamptz, PRIMARY KEY(item_id,user_id))")
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {schema}.items(id,user_id,platform,source,title,visible,"
                "relevance_score,fetched_at,published_at,ai_categories,ai_category,metrics_json) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows,
            )
        conn.execute(f"INSERT INTO {schema}.item_status(item_id,user_id,starred_at,hidden_at) "
                     "VALUES('twitter-0000','owner-a',now(),now())")
        conn.execute(f"CREATE INDEX source_sort_idx ON {schema}.items (platform, "
                     "COALESCE(published_at,fetched_at) DESC NULLS LAST, "
                     "fetched_at DESC NULLS LAST, relevance_score DESC NULLS LAST, id DESC) "
                     "WHERE visible=1")
        conn.commit()
    try:
        yield SimpleNamespace(connect=connect, schema=schema, now=now, platforms=platforms)
    finally:
        remote_db._cache_clear_all()
        with connect() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


def _overview(**kwargs):
    return remote_db.query_feed_platforms(per_platform=20, force_refresh=True, **kwargs)


def _reference_ids(pg, *, category=None):
    with pg.connect() as conn:
        category_filter = ""
        params = {}
        if category == remote_db.UNCATEGORIZED_SENTINEL:
            category_filter = " AND i.ai_categories IS NULL"
        elif category:
            category_filter = " AND i.ai_categories @> %(category)s::jsonb"
            params["category"] = '["' + category + '"]'
        rows = conn.execute(
            f"SELECT i.id FROM {pg.schema}.items i WHERE i.platform='twitter' "
            "AND i.visible=1 AND i.user_id IS NULL "
            "AND i.source != 'bookmarks' AND i.source NOT LIKE 'search:%%' "
            "AND i.fetched_at >= now()-interval '90 days' "
            + category_filter + " " + remote_db._PLATFORM_LIVE_ORDER, params,
        ).fetchall()
        return [row["id"] for row in rows]


def test_source_snapshot_overview_visibility_and_multicategory(source_postgres):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    public = _overview(public_only=True)
    assert set(public["sections"]) == set(pg.platforms)
    assert all(len(items) == 20 for items in public["sections"].values())
    assert public["platform_counts"]["twitter"] == 141
    assert public["category_counts"]["twitter"] == {
        "products": 47, "coding": 140, remote_db.UNCATEGORIZED_SENTINEL: 1,
    }
    assert public["counts_generated_at"]
    assert len(public["platform_next_cursors"]) == 7
    assert all(item["user_id"] is None for items in public["sections"].values() for item in items)
    private = _overview(user_id="owner-a", manual_owner_user_id="owner-a")
    assert private["platform_counts"]["manual"] == 15
    assert {item["user_id"] for item in private["sections"]["manual"]} == {"owner-a"}
    user_item = next(item for item in private["sections"]["twitter"]
                     if item["id"] == "twitter-0000")
    assert user_item["starred_at"]
    # Preserve the existing personal-status behavior: hidden_at is returned as
    # state, while global visible=0 is the list exclusion rule.
    assert user_item["hidden_at"]


def test_source_snapshot_failed_refresh_is_atomic(source_postgres):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    query = f"SELECT kind,value,n,computed_at FROM {pg.schema}.info_pill_counts ORDER BY kind,value"
    with pg.connect() as conn:
        before = conn.execute(query).fetchall()
        conn.execute(f"CREATE FUNCTION {pg.schema}.fail_counts() RETURNS trigger "
                     "LANGUAGE plpgsql AS $$ BEGIN IF NEW.kind='platform_meta' THEN "
                     "RAISE EXCEPTION 'test snapshot rollback'; END IF; RETURN NEW; END $$")
        conn.execute(f"CREATE TRIGGER fail_counts BEFORE INSERT OR UPDATE ON {pg.schema}.info_pill_counts "
                     f"FOR EACH ROW EXECUTE FUNCTION {pg.schema}.fail_counts()")
        conn.commit()
    cached_key = ('feed_platforms_result', pg.schema, 'preserved-on-failure')
    remote_db._cache_set(cached_key, {'successful': True})
    assert remote_db.refresh_info_pill_counts()["ok"] is False
    assert remote_db._cache_get(cached_key) == {'successful': True}
    with pg.connect() as conn:
        assert conn.execute(query).fetchall() == before


def test_source_snapshot_refresh_replaces_cached_totals_without_forced_reload(source_postgres):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    first = remote_db.query_feed_platforms(per_platform=20, public_only=True)
    old_total = first["platform_counts"]["twitter"]
    type_key = ('feed_sections_result', pg.schema)
    fallback_key = ('platforms_last_success', 'retained')
    remote_db._cache_set(type_key, {'unchanged': True})
    remote_db._cache_set(fallback_key, {'unchanged': True})
    legacy_key = remote_db._platform_page_count_cache_key(schema=pg.schema, platform='twitter', public_only=True, min_github_stars=50)
    remote_db._cache_set(legacy_key, old_total)
    with pg.connect() as conn:
        conn.execute(f"INSERT INTO {pg.schema}.items(id,platform,source,visible,fetched_at,published_at,"
                     "ai_category,ai_categories) VALUES('new-after-cached-count','twitter','following',1,now(),now(),"
                     "'products','[\"products\"]'::jsonb)")
        conn.commit()
    assert remote_db.refresh_info_pill_counts()["ok"]
    fresh = remote_db.query_feed_platforms(per_platform=20, public_only=True)
    assert fresh["platform_counts"]["twitter"] == old_total + 1
    assert 'new-after-cached-count' in {item['id'] for item in fresh['sections']['twitter']}
    assert remote_db._cache_get(legacy_key) is None
    assert remote_db._cache_get(type_key) == {'unchanged': True}
    assert remote_db._cache_get(fallback_key) == {'unchanged': True}


def test_source_keyset_exact_timestamp_precision_and_null_order(source_postgres):
    pg = source_postgres
    # Exercise NULL fetched timestamps too, although the live 90-day filter
    # excludes them. The predicate must still implement DESC NULLS LAST exactly.
    with pg.connect() as conn:
        conn.execute(f"CREATE TABLE {pg.schema}.sort_items (id text PRIMARY KEY, "
                     "published_at timestamptz,fetched_at timestamptz,relevance_score double precision)")
        rows = []
        for published in (None, pg.now, pg.now - timedelta(days=1)):
            for fetched in (None, pg.now, pg.now - timedelta(days=2)):
                for score in (None, 0.0, 1.0):
                    for _ in range(2):
                        rows.append((f"id-{len(rows):03d}", published, fetched, score))
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {pg.schema}.sort_items VALUES(%s,%s,%s,%s)", rows)
        ordered = conn.execute(f"SELECT i.* FROM {pg.schema}.sort_items i "
                               + remote_db._PLATFORM_LIVE_ORDER).fetchall()
        expected = [row["id"] for row in ordered]
        for index, row in enumerate(ordered):
            cursor = remote_db._platform_live_cursor(dict(row), "test-scope")
            after = remote_db._validate_platform_live_cursor(cursor, "test-scope")
            where, params = [], {}
            remote_db._add_platform_live_after(where, params, after)
            actual = conn.execute(f"SELECT i.id FROM {pg.schema}.sort_items i "
                                  + remote_db._where_sql(where) + " "
                                  + remote_db._PLATFORM_LIVE_ORDER, params).fetchall()
            assert [item["id"] for item in actual] == expected[index + 1:]


@pytest.mark.parametrize("category", [None, "products", remote_db.UNCATEGORIZED_SENTINEL])
def test_source_pages_reach_cold_rows_without_gaps(source_postgres, category):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    expected = _reference_ids(pg, category=category)
    if category is None:
        first = _overview(public_only=True)
        seen = [item["id"] for item in first["sections"]["twitter"]]
        cursor = first["platform_next_cursors"]["twitter"]
    else:
        first = remote_db.query_feed_by_platform(
            platform="twitter", category=category, limit=50, public_only=True,
        )
        seen = [item["id"] for item in first["items"]]
        cursor = first.get("next_cursor")
    for _ in range(10):
        if cursor is None:
            break
        page = remote_db.query_feed_by_platform(
            platform="twitter", category=category, limit=50, cursor=cursor, public_only=True,
        )
        seen.extend(item["id"] for item in page["items"])
        assert bool(page.get("next_cursor")) == page["has_more"]
        cursor = page.get("next_cursor")
    assert cursor is None
    assert seen == expected
    assert len(seen) == len(set(seen))
    assert "old" not in seen
    if category is None:
        assert "twitter-0139" in seen  # fetched 69 days ago, outside the hot model.


def test_source_page_new_top_item_and_legacy_offset(source_postgres):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    first = _overview(public_only=True)
    expected = _reference_ids(pg)
    first_ids = [item["id"] for item in first["sections"]["twitter"]]
    assert first_ids == expected[:20]
    with pg.connect() as conn:
        conn.execute(f"INSERT INTO {pg.schema}.items(id,platform,source,visible,fetched_at,published_at,ai_categories) "
                     "VALUES('new-top','twitter','following',1,now()+interval '1 minute',"
                     "now()+interval '1 minute','[\"coding\"]'::jsonb)")
        conn.commit()
    page = remote_db.query_feed_by_platform(
        platform="twitter", limit=50, public_only=True,
        cursor=first["platform_next_cursors"]["twitter"],
    )
    assert [item["id"] for item in page["items"]] == expected[20:70]
    assert "new-top" not in [item["id"] for item in page["items"]]
    legacy = remote_db.query_feed_by_platform(platform="twitter", offset=20, limit=50, public_only=True)
    assert [item["id"] for item in legacy["items"]] == _reference_ids(pg)[20:70]


def test_source_safe_cached_cards_and_expiry(source_postgres):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    fresh = _overview(public_only=True)
    hidden_id = fresh["sections"]["twitter"][0]["id"]
    with pg.connect() as conn:
        conn.execute(f"UPDATE {pg.schema}.items SET visible=0 WHERE id=%s", (hidden_id,))
        conn.commit()
    where, params = remote_db._base_item_where(public_only=True, min_github_stars=50)
    where.extend(["i.visible=1", "i.fetched_at >= now()-interval '90 days'"])
    remote_db._add_ai_relevance_filter(where)
    result = remote_db._validated_platforms_fallback(
        fresh, schema=pg.schema, where=where, params=params, user_id=None,
    )
    assert result["stale"] and result["degraded"]
    assert hidden_id not in [item["id"] for items in result["sections"].values() for item in items]
    assert result["platform_counts"] == {} and result["counts_generated_at"] is None
    expired = copy.deepcopy(fresh)
    expired["overview_generated_at"] = (pg.now - timedelta(hours=25)).isoformat()
    assert remote_db._validated_platforms_fallback(
        expired, schema=pg.schema, where=where, params=params, user_id=None,
    ) is None


def test_source_cards_survive_missing_snapshot(source_postgres):
    result = _overview(public_only=True)
    assert sum(map(len, result["sections"].values())) == 140
    assert result["counts_generated_at"] is None
    assert result["platform_counts"] == {}
    page = remote_db.query_feed_by_platform(platform="twitter", limit=50, public_only=True)
    assert len(page["items"]) == 50
    assert page["has_more"] and page["next_cursor"]
    assert page["total"] is None


def test_source_private_pages_and_cursor_scope(source_postgres):
    assert remote_db.refresh_info_pill_counts()["ok"]
    first = remote_db.query_feed_by_platform(
        platform="manual", limit=5, user_id="owner-a", manual_owner_user_id="owner-a",
    )
    assert first["total"] == 15
    assert {item["user_id"] for item in first["items"]} == {"owner-a"}
    second = remote_db.query_feed_by_platform(
        platform="manual", limit=5, user_id="owner-a", manual_owner_user_id="owner-a",
        cursor=first["next_cursor"],
    )
    assert {item["user_id"] for item in second["items"]} == {"owner-a"}
    assert not ({item["id"] for item in first["items"]}
                & {item["id"] for item in second["items"]})
    with pytest.raises(ValueError, match="scope"):
        remote_db.query_feed_by_platform(
            platform="manual", limit=5, user_id="owner-b", manual_owner_user_id="owner-b",
            cursor=first["next_cursor"],
        )
    anonymous = remote_db.query_feed_by_platform(platform="manual", limit=50, public_only=True)
    assert anonymous["items"] == []
    owner_b = _overview(user_id="owner-b", manual_owner_user_id="owner-b")
    assert owner_b["platform_counts"]["twitter"] == 142
    assert "stranger" in {item["id"] for item in owner_b["sections"]["twitter"]}


def test_source_ninety_day_boundary_uses_fetched_time_consistently(source_postgres):
    pg = source_postgres
    with pg.connect() as conn:
        conn.execute(f"INSERT INTO {pg.schema}.items(id,platform,source,visible,fetched_at,"
                     "published_at,ai_categories) VALUES "
                     "('inside-90d','twitter','following',1,now()-interval '90 days'+interval '1 minute',"
                     "now(),'[\"products\"]'::jsonb),"
                     "('outside-90d','twitter','following',1,now()-interval '90 days'-interval '1 minute',"
                     "now(),'[\"products\"]'::jsonb)")
        conn.commit()
    assert remote_db.refresh_info_pill_counts()["ok"]
    first = _overview(public_only=True)
    assert first["platform_counts"]["twitter"] == 142
    assert first["category_counts"]["twitter"]["products"] == 48
    first_ids = {item["id"] for item in first["sections"]["twitter"]}
    assert "inside-90d" in first_ids and "outside-90d" not in first_ids
    page = remote_db.query_feed_by_platform(platform="twitter", category="products", limit=50, public_only=True)
    page_ids = {item["id"] for item in page["items"]}
    assert "inside-90d" in page_ids and "outside-90d" not in page_ids
    assert page["total"] == 48


def test_source_configured_star_threshold_keeps_type_snapshot_policy(source_postgres, monkeypatch):
    pg = source_postgres
    assert remote_db.refresh_info_pill_counts()["ok"]
    query = f"SELECT n FROM {pg.schema}.info_pill_counts WHERE kind='section_category' AND value='products'"
    with pg.connect() as conn:
        type_count_before = conn.execute(query).fetchone()["n"]
        conn.execute(f"INSERT INTO {pg.schema}.items(id,platform,source,visible,fetched_at,published_at,"
                     "ai_category,ai_categories,metrics_json) VALUES('github-60-stars','github','repo',1,now(),now(),"
                     "'products','[\"products\"]'::jsonb,'{\"stars\":60}'::jsonb)")
        conn.commit()
    monkeypatch.setattr(remote_db, "_source_display_min_stars", lambda: 75)
    assert remote_db.refresh_info_pill_counts()["ok"]
    source = _overview(public_only=True, min_github_stars=75)
    assert source["counts_generated_at"]
    assert source["platform_counts"]["github"] == 140
    assert "github-60-stars" not in {item["id"] for item in source["sections"]["github"]}
    page = remote_db.query_feed_by_platform(platform="github", limit=50, public_only=True, min_github_stars=75)
    assert page["total"] == 140
    assert "github-60-stars" not in {item["id"] for item in page["items"]}
    with pg.connect() as conn:
        assert conn.execute(query).fetchone()["n"] == type_count_before + 1
