from contextlib import contextmanager
import json
import sys
import types

import pytest

import remote_db


_ORIGINAL_READ_LOCAL_READ_CACHE = remote_db._read_local_read_cache


@pytest.fixture(autouse=True)
def _disable_local_read_cache(monkeypatch):
    monkeypatch.setattr(remote_db, "_read_local_read_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL", 0.0)
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_ENV, "0")


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeOneResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.sqls = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        return _FakeResult(self.rows)


class _FailingConn:
    def __init__(self):
        self.sqls = []
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if " ".join(sql.split()).startswith("SET LOCAL"):
            return _FakeResult([])
        raise RuntimeError("remote query timeout")

    def rollback(self):
        self.rollbacks += 1


class _FakeOneConn:
    def __init__(self, row):
        self.row = row
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        return _FakeOneResult(self.row)


class _QueryFeedConn:
    def __init__(self, *, rows=None, count=123, fail_items=False, fail_count=False):
        self.rows = rows if rows is not None else [_row("tw_1")]
        self.count = count
        self.fail_items = fail_items
        self.fail_count = fail_count
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "SELECT count(*) AS n" in normalized:
            if self.fail_count:
                raise RuntimeError("remote count timeout")
            return _FakeOneResult({"n": self.count})
        if self.fail_items:
            raise RuntimeError("remote item timeout")
        return _FakeResult(self.rows)


class _InfoReadModelConn:
    def __init__(self):
        self.sqls = []
        self.params = []
        self.commits = 0
        self.version = "00000000-0000-0000-0000-000000000001"
        self.generated_at = "2026-05-22T08:00:00+00:00"
        self.max_fetched_at = "2026-05-22T07:55:00+00:00"

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if (
            "FROM remote_poc.info_scopes sc" in normalized
            and "sc.dimension = 'section_category'" in normalized
            and "GROUP BY sc.value" in normalized
        ):
            return _FakeResult([
                {"category": "products", "total_count": 2, "max_sort_at": self.max_fetched_at},
            ])
        if (
            "info_scope_items" in normalized
            and "JOIN remote_poc.info_card_items" in normalized
            and "sc.dimension = 'section_category'" in normalized
            and "LIMIT %(limit)s" in normalized
        ):
            return _FakeResult([
                {
                    "category": "products",
                    "card_json": _row(
                        "prod_recent",
                        category="products",
                        ai_categories=["products", "tech"],
                        fetched_at="2026-05-23T00:00:00+00:00",
                        published_at="2020-01-01T00:00:00+00:00",
                    ),
                    "rank": 1,
                    "fetched_at": "2026-05-23T00:00:00+00:00",
                    "relevance_score": 1,
                    "item_id": "prod_recent",
                },
                {
                    "category": "products",
                    "card_json": _row(
                        "prod_older",
                        category="products",
                        ai_categories=["products"],
                        fetched_at="2026-05-22T00:00:00+00:00",
                        published_at="2026-05-24T00:00:00+00:00",
                    ),
                    "rank": 2,
                    "fetched_at": "2026-05-22T00:00:00+00:00",
                    "relevance_score": 1,
                    "item_id": "prod_older",
                },
            ])
        if (
            "FROM remote_poc.info_scopes sc" in normalized
            and "sc.dimension = 'category'" in normalized
            and "GROUP BY sc.value" in normalized
        ):
            return _FakeResult([
                {"category": "products", "total_count": 70, "max_sort_at": self.max_fetched_at},
                {"category": "ai", "total_count": 90, "max_sort_at": self.max_fetched_at},
            ])
        if (
            "info_scope_items" in normalized
            and "JOIN remote_poc.info_card_items" in normalized
            and "sc.dimension = 'category'" in normalized
            and "ORDER BY si.sort_at DESC NULLS LAST" in normalized
        ):
            return _FakeResult([
                {
                    "category": "products",
                    "card_json": _row("prod_rm_1", source="following", category="products"),
                    "rank": 1,
                    "sort_at": self.max_fetched_at,
                    "fetched_at": self.max_fetched_at,
                    "relevance_score": 1,
                    "item_id": "prod_rm_1",
                },
                {
                    "category": "ai",
                    "card_json": _row("tw_rm_1", source="following", category="ai"),
                    "rank": 1,
                    "sort_at": self.max_fetched_at,
                    "fetched_at": self.max_fetched_at,
                    "relevance_score": 1,
                    "item_id": "tw_rm_1",
                },
            ])
        if (
            "WITH active_version AS" in normalized
            and "page_rows AS" in normalized
            and "section_subcategory" in (params or {}).get("scope_key", "")
        ):
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 1,
                    "total_count": 80,
                    "card_json": _row(
                        f"prod_rm_{(params or {}).get('offset', 0) + 1}",
                        category="products",
                        ai_categories=["products"],
                    ),
                    "rank": (params or {}).get("offset", 0) + 1,
                    "fetched_at": self.max_fetched_at,
                    "relevance_score": 1,
                    "item_id": f"prod_rm_{(params or {}).get('offset', 0) + 1}",
                }
            ])
        if (
            "WITH active_version AS" in normalized
            and "page_rows AS" in normalized
            and "section_category" in (params or {}).get("scope_key", "")
        ):
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 1,
                    "total_count": 80,
                    "card_json": _row(
                        f"prod_rm_{(params or {}).get('offset', 0) + 1}",
                        category="products",
                        ai_categories=["products"],
                    ),
                    "rank": (params or {}).get("offset", 0) + 1,
                    "fetched_at": self.max_fetched_at,
                    "relevance_score": 1,
                    "item_id": f"prod_rm_{(params or {}).get('offset', 0) + 1}",
                }
            ])
        if "WITH active_version AS" in normalized and "page_rows AS" in normalized:
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 1,
                    "total_count": 80,
                    "card_json": _row("tw_rm_51", source="following", category="ai"),
                    "rank": (params or {}).get("offset", 0) + 1,
                }
            ])
        if "FROM remote_poc.info_read_model_state" in normalized:
            return _FakeOneResult({
                "version_id": self.version,
                "generated_at": self.generated_at,
                "max_fetched_at": self.max_fetched_at,
            })
        if (
            "FROM remote_poc.info_scopes sc" in normalized
            and "GROUP BY sc.platform, sc.dimension, sc.value" in normalized
        ):
            return _FakeResult([
                {"platform": "twitter", "dimension": "all", "value": "", "total_count": 120, "max_sort_at": self.max_fetched_at},
                {"platform": "twitter", "dimension": "source", "value": "following", "total_count": 80, "max_sort_at": self.max_fetched_at},
                {"platform": "twitter", "dimension": "category", "value": "ai", "total_count": 90, "max_sort_at": self.max_fetched_at},
            ])
        if "FROM remote_poc.info_scopes sc" in normalized and "JOIN remote_poc.info_scope_items" not in normalized:
            return _FakeResult([
                {"platform": "twitter", "dimension": "all", "value": "", "total_count": 120, "max_sort_at": self.max_fetched_at},
                {"platform": "twitter", "dimension": "source", "value": "following", "total_count": 80, "max_sort_at": self.max_fetched_at},
                {"platform": "twitter", "dimension": "category", "value": "ai", "total_count": 90, "max_sort_at": self.max_fetched_at},
                {"platform": "lingowhale", "dimension": "group", "value": "AI周刊", "total_count": 12, "max_sort_at": self.max_fetched_at},
                {"platform": "lingowhale", "dimension": "group_source", "value": "AI周刊::AI产品-公众号", "total_count": 8, "max_sort_at": self.max_fetched_at},
            ])
        if "info_scope_items" in normalized and "remote_poc.info_card_items" in normalized:
            requested_scope = (params or {}).get("scope_key")
            if requested_scope:
                return _FakeResult([
                    {
                        "card_json": _row("tw_rm_51", source="following", category="ai"),
                        "rank": 51,
                    }
                ])
            return _FakeResult([
                {
                    "platform": "twitter",
                    "card_json": _row("tw_rm_1", source="following", category="ai"),
                    "rank": 1,
                }
            ])
        if "FROM remote_poc.info_scopes" in normalized and "scope_key = %(scope_key)s" in normalized:
            return _FakeOneResult({
                "total_count": 80,
                "generated_at": self.generated_at,
                "max_fetched_at": self.max_fetched_at,
            })
        raise AssertionError(f"unexpected SQL: {normalized}")

    def commit(self):
        self.commits += 1


class _InfoSearchReadModelConn(_InfoReadModelConn):
    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        scope_key = (params or {}).get("scope_key", "")
        if (
            "PARTITION BY mc.category" in normalized
            and "matched_cards AS MATERIALIZED" in normalized
            and "search_like" in normalized
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {
                    "category": "products",
                    "total_count": 2,
                    "rn": 1,
                    "card_json": _row("prod_search_1", category="products", ai_categories=["products"]),
                }
            ])
        if (
            "PARTITION BY mc.platform" in normalized
            and "matched_cards AS MATERIALIZED" in normalized
            and "search_like" in normalized
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {
                    "platform": "twitter",
                    "total_count": 2,
                    "rn": 1,
                    "card_json": _row("tw_search_1", source="following", category="ai"),
                }
            ])
        if (
            "SELECT platform, source, count(DISTINCT item_id)::integer AS cnt" in normalized
            and "GROUP BY platform, source" in normalized
            and "search_like" in normalized
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {"platform": "twitter", "source": "following", "cnt": 2},
            ])
        if (
            "matched_categories AS" in normalized
            and "GROUP BY platform, category" in normalized
            and "search_like" in normalized
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {"platform": "twitter", "category": "ai", "cnt": 2},
            ])
        if (
            "summary.scope_count" in normalized
            and "search_like" in normalized
            and "dimension=section_category" in scope_key
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 1,
                    "total_count": 2,
                    "rank": (params or {}).get("offset", 0) + 1,
                    "item_id": "prod_search_page",
                    "card_json": _row("prod_search_page", category="products", ai_categories=["products"]),
                }
            ])
        if (
            "summary.scope_count" in normalized
            and "search_like" in normalized
            and "platform=twitter" in scope_key
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 1,
                    "total_count": 2,
                    "rank": 2,
                    "card_json": _row("tw_search_page", source="following", category="ai"),
                }
            ])
        return super().execute(sql, params)


class _InfoSearchReadModelTimeoutConn(_InfoReadModelConn):
    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "FROM remote_poc.info_read_model_state" in normalized:
            return _FakeOneResult({
                "version_id": self.version,
                "generated_at": self.generated_at,
                "max_fetched_at": self.max_fetched_at,
            })
        if "search_like" in normalized and "info_scope_items" in normalized:
            raise RuntimeError("search read model timeout")
        if "FROM remote_poc.items i" in normalized:
            raise AssertionError("search read model timeout must not fall back to live items")
        return super().execute(sql, params)


class _InfoReadModelUserConn(_InfoReadModelConn):
    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM remote_poc.item_status" in normalized:
            self.sqls.append(sql)
            self.params.append(params or {})
            return _FakeResult([
                {
                    "item_id": "prod_recent",
                    "read_at": None,
                    "clicked_at": "2026-05-23T01:02:03+00:00",
                    "starred_at": "2026-05-23T01:03:04+00:00",
                    "hidden_at": None,
                },
                {
                    "item_id": "prod_rm_1",
                    "read_at": None,
                    "clicked_at": "2026-05-23T02:02:03+00:00",
                    "starred_at": None,
                    "hidden_at": None,
                },
            ])
        return super().execute(sql, params)


class _InfoReadModelManualOverlayConn(_InfoReadModelUserConn):
    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "i.platform = 'manual'" in normalized:
            self.sqls.append(sql)
            self.params.append(params or {})
            if "SELECT count(*) AS cnt FROM remote_poc.items i" in normalized:
                return _FakeOneResult({"cnt": 1})
            if "SELECT COALESCE(i.source, '') AS source, count(*) AS cnt" in normalized:
                return _FakeResult([{"source": "user-submit", "cnt": 1}])
            if "CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories)" in normalized:
                return _FakeResult([{"category": "products", "cnt": 1}])
            if "GROUP BY 1" in normalized:
                return _FakeResult([{"category": "products", "cnt": 1}])
            if "WITH ranked AS" in normalized:
                row = _row(
                    "manual_private",
                    platform="manual",
                    source="user-submit",
                    category="products",
                    ai_categories=["products"],
                    fetched_at="2026-05-23T09:00:00+00:00",
                )
                row["section_category"] = "products"
                row["rn"] = 1
                row["user_id"] = "user-1"
                return _FakeResult([row])
            row = _row(
                "manual_private",
                platform="manual",
                source="user-submit",
                category="products",
                ai_categories=["products"],
                fetched_at="2026-05-23T09:00:00+00:00",
            )
            row["user_id"] = "user-1"
            return _FakeResult([row])
        return super().execute(sql, params)


class _InfoReadModelLiveOverlayConn(_InfoReadModelConn):
    def __init__(self, rows):
        super().__init__()
        self.overlay_rows = rows

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if (
            "FROM remote_poc.items i" in normalized
            and "i.fetched_at > %(overlay_after)s::timestamptz" in normalized
        ):
            self.sqls.append(sql)
            self.params.append(params or {})
            rows = []
            for row in self.overlay_rows:
                data = dict(row)
                data.setdefault("section_category", remote_db._section_category_from_row(data))
                rows.append(data)
            return _FakeResult(rows)
        return super().execute(sql, params)


class _InfoReadModelMissingScopeConn:
    def __init__(self):
        self.sqls = []
        self.params = []
        self.version = "00000000-0000-0000-0000-000000000002"
        self.generated_at = "2026-05-23T02:22:31+00:00"
        self.max_fetched_at = "2026-05-23T02:08:05+00:00"

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "WITH active_version AS" in normalized and "page_rows AS" in normalized:
            return _FakeResult([
                {
                    "version_id": self.version,
                    "generated_at": self.generated_at,
                    "max_fetched_at": self.max_fetched_at,
                    "scope_count": 0,
                    "total_count": 0,
                    "card_json": None,
                    "rank": None,
                    "fetched_at": None,
                    "relevance_score": None,
                    "item_id": None,
                }
            ])
        if "SELECT count(*) AS n FROM remote_poc.items i" in normalized:
            return _FakeOneResult({"n": 682})
        if "FROM remote_poc.items i" in normalized:
            return _FakeResult([
                _row(
                    "prod_live_1",
                    category="products",
                    ai_categories=["products"],
                    ai_subcategories=["chatbot"],
                )
            ])
        raise AssertionError(f"unexpected SQL: {normalized}")


class _InfoReadModelBuildConn:
    def __init__(self):
        self.sqls = []
        self.params = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "SELECT count(*) AS n FROM remote_poc.info_scope_items" in normalized:
            return _FakeOneResult({"n": 321})
        if "SELECT count(*) AS n FROM remote_poc.info_card_items" in normalized:
            return _FakeOneResult({"n": 123})
        return _FakeResult([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _InfoReadModelIncrementalConn(_InfoReadModelBuildConn):
    def __init__(self):
        super().__init__()
        self.active_version = "00000000-0000-0000-0000-000000000001"
        self.active_max = "2026-05-24T01:00:00+00:00"
        self.delta_max = "2026-05-24T02:00:00+00:00"

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "FROM remote_poc.info_read_model_state s" in normalized:
            return _FakeOneResult({
                "version_id": self.active_version,
                "generated_at": "2026-05-24T01:00:00+00:00",
                "max_fetched_at": self.active_max,
                "meta_json": {"sort_policy": remote_db.INFO_READ_MODEL_SORT_POLICY},
            })
        if "SELECT count(*) AS n, max(fetched_at) AS max_fetched_at FROM pg_temp.info_read_model_delta" in normalized:
            return _FakeOneResult({"n": 2, "max_fetched_at": self.delta_max})
        if "SELECT count(*) AS n FROM remote_poc.info_scope_items" in normalized:
            return _FakeOneResult({"n": 330})
        if "SELECT count(*) AS n FROM remote_poc.info_card_items" in normalized:
            return _FakeOneResult({"n": 125})
        return _FakeResult([])


class _InfoReadModelSortPolicyMigrationConn(_InfoReadModelBuildConn):
    def __init__(self):
        super().__init__()
        self.active_version = "00000000-0000-0000-0000-000000000001"
        self.active_max = "2026-05-24T01:00:00+00:00"

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "FROM remote_poc.info_read_model_state s" in normalized:
            return _FakeOneResult({
                "version_id": self.active_version,
                "generated_at": "2026-05-24T01:00:00+00:00",
                "max_fetched_at": self.active_max,
                "meta_json": {"sort_policy": "fetched_at_desc_legacy"},
            })
        if "SELECT count(*) AS n FROM pg_temp.info_read_model_reranked_scope_items" in normalized:
            return _FakeOneResult({"n": 330})
        return _FakeResult([])


class _InfoReadModelFreshnessConn:
    def __init__(
        self,
        *,
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T01:00:00+00:00",
        active_sort_policy=None,
    ):
        self.active_max = active_max
        self.latest_max = latest_max
        self.active_sort_policy = (
            remote_db.INFO_READ_MODEL_SORT_POLICY
            if active_sort_policy is None
            else active_sort_policy
        )
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "FROM remote_poc.info_read_model_state s" in normalized:
            return _FakeOneResult({
                "version_id": "00000000-0000-0000-0000-000000000001",
                "generated_at": "2026-05-24T00:00:00+00:00",
                "max_fetched_at": self.active_max,
                "meta_json": {"sort_policy": self.active_sort_policy},
            })
        if (
            "SELECT i.fetched_at AS latest_fetched_at" in normalized
            and "ORDER BY i.fetched_at DESC NULLS LAST LIMIT 1" in normalized
        ):
            return _FakeOneResult({"latest_fetched_at": self.latest_max})
        raise AssertionError(f"unexpected SQL: {normalized}")


class _InfoReadModelPrewarmConn(_InfoReadModelConn):
    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params or {})
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "FROM remote_poc.info_read_model_state" in normalized:
            return _FakeOneResult({
                "version_id": self.version,
                "generated_at": self.generated_at,
                "max_fetched_at": self.max_fetched_at,
                "meta_json": {"sort_policy": remote_db.INFO_READ_MODEL_SORT_POLICY},
            })
        if "hot_scopes AS" in normalized:
            return _FakeResult([
                {
                    "platform": "twitter",
                    "dimension": "source",
                    "value": "following",
                    "scope_key": "platform=twitter|dimension=source|value=following",
                    "total_count": 80,
                    "card_json": _row("tw_hot_1", source="following", category="ai"),
                    "rank": 1,
                },
                {
                    "platform": "twitter",
                    "dimension": "source",
                    "value": "following",
                    "scope_key": "platform=twitter|dimension=source|value=following",
                    "total_count": 80,
                    "card_json": _row("tw_hot_2", source="following", category="ai"),
                    "rank": 2,
                },
                {
                    "platform": "twitter",
                    "dimension": "source",
                    "value": "following",
                    "scope_key": "platform=twitter|dimension=source|value=following",
                    "total_count": 80,
                    "card_json": _row("tw_hot_51", source="following", category="ai"),
                    "rank": 51,
                },
                {
                    "platform": "twitter",
                    "dimension": "source",
                    "value": "following",
                    "scope_key": "platform=twitter|dimension=source|value=following",
                    "total_count": 80,
                    "card_json": _row("tw_hot_52", source="following", category="ai"),
                    "rank": 52,
                },
            ])
        return super().execute(sql, params)


class _PlatformOverviewConn:
    def __init__(
        self,
        *,
        item_rows=None,
        platform_rows=None,
        source_rows=None,
        category_rows=None,
        null_category_rows=None,
    ):
        self.item_rows = item_rows if item_rows is not None else [_row("tw_1")]
        self.platform_rows = platform_rows if platform_rows is not None else [
            {"platform": "twitter", "cnt": len(self.item_rows)}
        ]
        self.source_rows = source_rows if source_rows is not None else [
            {"platform": "twitter", "source": "following", "cnt": len(self.item_rows)}
        ]
        self.category_rows = category_rows if category_rows is not None else [
            {"platform": "twitter", "category": "ai", "cnt": len(self.item_rows)}
        ]
        self.null_category_rows = null_category_rows if null_category_rows is not None else []
        self.sqls = []
        self.params = []
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self.params.append(params)
        normalized = " ".join(sql.split())
        if normalized.startswith("SET LOCAL"):
            return _FakeResult([])
        if "GROUP BY i.platform, i.source" in normalized:
            return _FakeResult(self.source_rows)
        if "GROUP BY i.platform, cat.value" in normalized:
            return _FakeResult(self.category_rows)
        if "AS section_category, count(*) AS cnt" in normalized:
            return _FakeResult(self.category_rows)
        if "i.ai_categories IS NULL" in normalized:
            return _FakeResult(self.null_category_rows)
        if "GROUP BY i.platform" in normalized:
            return _FakeResult(self.platform_rows)
        return _FakeResult(self.item_rows)

    def rollback(self):
        self.rollbacks += 1


class _FakeRefreshResult:
    def __init__(self, row=None):
        self._row = row or {"n": 453}

    def fetchone(self):
        return self._row


class _FakeRefreshConn:
    def __init__(self, *, fail_concurrent=False):
        self.fail_concurrent = fail_concurrent
        self.sqls = []
        self.rollbacks = 0
        self.autocommit = False

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if "CONCURRENTLY" in sql and self.fail_concurrent:
            raise RuntimeError("concurrent refresh busy")
        return _FakeRefreshResult()

    def rollback(self):
        self.rollbacks += 1


def _row(
    item_id,
    *,
    platform="twitter",
    source="following",
    category="ai",
    ai_categories=None,
    ai_subcategories=None,
    fetched_at="2026-05-16T00:00:00+00:00",
    published_at=None,
):
    return {
        "id": item_id,
        "user_id": None,
        "platform": platform,
        "source": source,
        "title": f"title {item_id}",
        "author_name": "author",
        "author_id": None,
        "author_avatar": None,
        "url": f"https://example.com/{item_id}",
        "cover_url": None,
        "media_json": None,
        "metrics_json": None,
        "tags_json": None,
        "lang": None,
        "detail_json": None,
        "comments_json": None,
        "content": None,
        "description": None,
        "ai_summary": "summary",
        "ai_key_points": None,
        "ai_category": category,
        "ai_keywords": None,
        "ai_categories": [category] if ai_categories is None else ai_categories,
        "ai_subcategories": ai_subcategories,
        "multi_l1_reason": None,
        "ai_extracted": None,
        "content_type": None,
        "visible": 1,
        "relevance_score": 1,
        "fetched_at": fetched_at,
        "published_at": published_at,
        "created_at": fetched_at,
        "read_at": None,
        "clicked_at": None,
        "starred_at": None,
        "hidden_at": None,
    }


def test_query_feed_platforms_live_path_uses_full_counts_not_sample_rows(monkeypatch):
    remote_db.clear_feed_cache_keys()
    min_github_stars = 9876
    conn = _PlatformOverviewConn(
        item_rows=[
            _row("tw_1", source="following", category="ai"),
            _row("tw_2", source="bookmarks", category="coding"),
            _row("gh_1", platform="github", source="trending", category="coding"),
        ],
        platform_rows=[
            {"platform": "twitter", "cnt": 80},
            {"platform": "github", "cnt": 8},
        ],
        source_rows=[
            {"platform": "twitter", "source": "following", "cnt": 80},
            {"platform": "github", "source": "trending", "cnt": 8},
        ],
        category_rows=[
            {"platform": "twitter", "category": "ai", "cnt": 80},
            {"platform": "github", "category": "coding", "cnt": 8},
        ],
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_platforms_mv_available", lambda _conn, _schema: True)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=min_github_stars,
    )

    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert not any("mv_items_top_per_platform" in sql for sql in conn.sqls)
    item_sql = next(sql for sql in conn.sqls if "WITH ranked AS" in sql)
    assert "WHERE rn <= %(per_platform)s" in item_sql
    assert "FROM remote_poc.items i" in item_sql
    assert "created_at, NULL::timestamptz AS read_at" in item_sql
    assert ",\n                              row_number() OVER" in item_sql
    assert "NULL::text AS ai_key_points" in item_sql
    assert "i.ai_key_points" not in item_sql
    assert "COALESCE(i.source, '') != 'bookmarks'" in item_sql
    assert "i.id DESC" in item_sql
    assert "id DESC" in item_sql
    assert result["sections"]["twitter"][0]["id"] == "tw_1"
    assert result["platform_counts"] == {"twitter": 80, "github": 8}
    assert result["source_counts"]["twitter"] == {"following": 80}
    assert result["category_counts"]["twitter"] == {"ai": 80}
    assert result["sample_limit"] == 50
    assert result["overview_generated_at"]
    assert result["overview_max_fetched_at"] == "2026-05-16T00:00:00Z"
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_uses_info_read_model_when_enabled(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["sections"]["twitter"][0]["id"] == "tw_rm_1"
    assert result["platform_counts"] == {"twitter": 120}
    assert result["source_counts"] == {"twitter": {"following": 80}}
    assert result["category_counts"] == {"twitter": {"ai": 90}}
    assert result["sample_limit"] == 50
    assert result["overview_generated_at"] == "2026-05-22T08:00:00Z"
    assert result["overview_max_fetched_at"] == "2026-05-22T07:55:00Z"
    assert result["data_backend"] == "supabase_poc"
    assert result["platform_next_cursors"]["twitter"] == {
        "version_id": conn.version,
        "scope_key": "platform=twitter|dimension=all|value=",
        "rank_after": 1,
    }
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert conn.sqls[1] == "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
    assert any("info_read_model_state" in sql for sql in conn.sqls)
    assert any("info_scope_items" in sql for sql in conn.sqls)
    card_sql = next(sql for sql in conn.sqls if "WITH all_scope_items AS MATERIALIZED" in sql)
    assert "WITH all_scope_items AS MATERIALIZED" in card_sql
    assert "CROSS JOIN LATERAL" in card_sql
    assert "si.scope_key = sc.scope_key" in card_sql
    assert "ci.item_id = page.item_id" in card_sql
    assert "COALESCE(ci.source, '') != 'bookmarks'" in card_sql
    assert "OFFSET 0" in card_sql
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)
    assert conn.commits >= 1

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_search_uses_info_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_search_v1"
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '8000ms'"
    assert result["sections"]["twitter"][0]["id"] == "tw_search_1"
    assert result["platform_counts"] == {"twitter": 2}
    assert result["source_counts"] == {"twitter": {"following": 2}}
    assert result["category_counts"] == {"twitter": {"ai": 2}}
    card_sql = next(sql for sql in conn.sqls if "PARTITION BY mc.platform" in sql and "search_like" in sql)
    assert "matched_cards AS MATERIALIZED" in card_sql
    assert "ci.search_text ILIKE %(search_like)s" in card_sql
    assert "info_scope_items" not in card_sql
    assert "ORDER BY mc.sort_at DESC NULLS LAST" in card_sql
    assert "pr.card_json" in card_sql
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_search_does_not_fall_back_to_live_items_when_read_model_times_out(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelTimeoutConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "info_search_read_model_unavailable"
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_logged_in_merges_private_manual_overlay(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelManualOverlayConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )

    assert result["read_model"] == "info_platforms_v1"
    assert result["private_manual_overlay"] is True
    assert result["platform_counts"]["manual"] == 1
    assert result["source_counts"]["manual"]["user-submit"] == 1
    assert result["category_counts"]["manual"]["products"] == 1
    assert result["sections"]["manual"][0]["id"] == "manual_private"
    assert any("i.platform = 'manual'" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_live_overlay_prepends_recent_items(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelLiveOverlayConn([
        _row(
            "tw_live_new",
            source="following",
            category="ai",
            fetched_at="2026-05-24T02:00:00+00:00",
            published_at="2026-05-24T02:00:00+00:00",
        )
    ])

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_LIVE_OVERLAY_ENV, "1")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_LIVE_OVERLAY_LIMIT_ENV, "10")

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["sections"]["twitter"][0]["id"] == "tw_live_new"
    assert result["sections"]["twitter"][1]["id"] == "tw_rm_1"
    assert result["platform_counts"]["twitter"] == 121
    assert result["source_counts"]["twitter"]["following"] == 81
    assert result["category_counts"]["twitter"]["ai"] == 91
    assert result["overview_max_fetched_at"] == "2026-05-24T02:00:00Z"
    assert result["live_overlay"] is True
    assert result["live_overlay_count"] == 1
    assert result["live_overlay_enabled"] is True
    assert result["live_overlay_after"] == "2026-05-22T07:55:00Z"
    assert result["live_overlay_limit"] == 10
    assert result["live_overlay_per_scope_limit"] == 10
    assert result["live_overlay_timeout_ms"] == 1500
    assert result["live_overlay_attempted"] is True
    assert result["live_overlay_latest_fetched_at"] == "2026-05-24T02:00:00Z"
    assert conn.commits >= 1
    assert "SET LOCAL idle_in_transaction_session_timeout = '5000ms'" in conn.sqls
    assert result["platform_next_cursors"]["twitter"] == {
        "version_id": conn.version,
        "scope_key": "platform=twitter|dimension=all|value=",
        "rank_after": 1,
        "exclude_ids": ["tw_live_new", "tw_rm_1"],
    }

    overlay_sql = next(sql for sql in conn.sqls if "FROM remote_poc.items i" in sql)
    assert "i.fetched_at > %(overlay_after)s::timestamptz" in overlay_sql
    assert "WITH recent AS MATERIALIZED" in overlay_sql
    assert "PARTITION BY recent.platform" in overlay_sql
    assert "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC" in overlay_sql
    assert "LIMIT %(overlay_limit)s" in overlay_sql
    assert "overlay_rn <= %(overlay_per_scope_limit)s" in overlay_sql
    assert not any("GROUP BY" in sql for sql in conn.sqls if "FROM remote_poc.items i" in sql)

    sql_count_after_first_call = len(conn.sqls)
    cached_probe = remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert cached_probe["live_overlay_enabled"] is True
    assert cached_probe["sections"]["twitter"][0]["id"] == "tw_live_new"
    assert len(conn.sqls) == sql_count_after_first_call

    remote_db.clear_feed_cache_keys()


def test_info_live_overlay_section_cursor_excludes_visible_ids():
    base_items = [
        _row(
            f"prod_rm_{idx}",
            category="products",
            ai_categories=["products"],
            fetched_at=f"2026-05-23T00:{idx:02d}:00+00:00",
        )
        for idx in range(1, 51)
    ]
    base_items[47]["id"] = "prod_late_dup"
    overlay_items = [
        _row(
            f"prod_live_{idx}",
            category="products",
            ai_categories=["products"],
            fetched_at=f"2026-05-24T02:{idx:02d}:00+00:00",
        )
        for idx in range(1, 6)
    ]
    overlay_items.append(
        _row(
            "prod_late_dup",
            category="products",
            ai_categories=["products"],
            fetched_at="2026-05-24T03:00:00+00:00",
        )
    )
    result = {
        "sections": {"products": base_items},
        "cat_counts": {"products": 80},
        "sample_limit": 50,
        "section_next_cursors": {
            "products": {
                "version_id": "version-1",
                "scope_key": "platform=_all|dimension=section_category|value=products",
                "rank_after": 50,
            }
        },
    }

    merged = remote_db._merge_info_live_overlay_sections(result, overlay_items)

    cursor = merged["section_next_cursors"]["products"]
    assert cursor["rank_after"] == 45
    assert "prod_late_dup" in cursor["exclude_ids"]
    assert "prod_live_1" in cursor["exclude_ids"]
    assert len(merged["sections"]["products"]) == 50


def test_info_live_overlay_merge_sorts_by_original_article_time():
    base_items = [
        _row(
            "published_newer",
            category="products",
            ai_categories=["products"],
            fetched_at="2026-05-23T00:00:00+00:00",
            published_at="2026-05-24T01:00:00+00:00",
        )
    ]
    overlay_items = [
        _row(
            "fetched_newer_but_published_older",
            category="products",
            ai_categories=["products"],
            fetched_at="2026-05-24T03:00:00+00:00",
            published_at="2026-05-20T00:00:00+00:00",
        )
    ]
    result = {
        "sections": {"products": base_items},
        "cat_counts": {"products": 2},
        "sample_limit": 50,
        "section_next_cursors": {},
    }

    merged = remote_db._merge_info_live_overlay_sections(result, overlay_items)

    assert [item["id"] for item in merged["sections"]["products"]] == [
        "published_newer",
        "fetched_newer_but_published_older",
    ]


def test_query_feed_by_category_cursor_excludes_live_overlay_duplicates(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=50,
        limit=50,
        cursor={
            "version_id": conn.version,
            "scope_key": "platform=_all|dimension=section_category|value=products",
            "rank_after": 45,
            "exclude_ids": ["prod_late_dup"],
        },
        keyword=None,
        search=None,
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    page_sql = next(sql for sql in conn.sqls if "WITH active_version AS" in sql and "page_rows AS" in sql)
    page_params = next(params for params in conn.params if (params or {}).get("exclude_ids") == ["prod_late_dup"])
    assert "NOT (si.item_id = ANY(%(exclude_ids)s))" in page_sql
    assert "LIMIT %(limit)s" in page_sql
    assert page_params["offset"] == 45
    assert result["next_cursor"]["exclude_ids"] == ["prod_late_dup"]

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_uses_info_read_model_when_enabled(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["sections"]["products"]] == ["prod_recent", "prod_older"]
    assert "tech" not in result["sections"]
    assert "ai" not in result["sections"]
    assert result["cat_counts"] == {"products": 2}
    assert result["total"] == 2
    assert result["sample_limit"] == 50
    assert result["overview_generated_at"] == "2026-05-22T08:00:00Z"
    assert result["overview_max_fetched_at"] == "2026-05-22T07:55:00Z"
    assert result["data_backend"] == "supabase_poc"
    assert result["read_model"] == "info_platforms_v1"
    assert result["section_next_cursors"]["products"] is None
    assert any("info_read_model_state" in sql for sql in conn.sqls)
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert conn.sqls[1] == "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
    assert any("sc.dimension = 'section_category'" in sql for sql in conn.sqls)
    assert any("CROSS JOIN LATERAL" in sql for sql in conn.sqls)
    assert any(
        "ORDER BY sr.category," in sql and "page.sort_at DESC NULLS LAST" in sql
        for sql in conn.sqls
    )
    assert not any("sc.dimension = 'category'" in sql for sql in conn.sqls)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)
    assert conn.commits >= 1

    @contextmanager
    def fail_connect():
        raise AssertionError("cached section page should not touch remote db")
        yield

    monkeypatch.setattr(remote_db, "connect", fail_connect)
    page = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in page["items"]] == ["prod_recent", "prod_older"]
    assert page["read_model"] == "info_platforms_v1"
    assert page["next_offset"] is None

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_live_overlay_prepends_recent_items(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelLiveOverlayConn([
        _row(
            "prod_recent",
            category="products",
            ai_categories=["products"],
            fetched_at="2026-05-24T02:10:00+00:00",
            published_at="2026-05-24T02:10:00+00:00",
        )
    ])

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_LIVE_OVERLAY_ENV, "1")
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_LIVE_OVERLAY_LIMIT_ENV, "10")

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["sections"]["products"][:3]] == [
        "prod_recent",
        "prod_older",
    ]
    assert result["sections"]["products"][0]["fetched_at"] == "2026-05-24T02:10:00Z"
    assert result["cat_counts"]["products"] == 2
    assert result["total"] == 2
    assert result["overview_max_fetched_at"] == "2026-05-24T02:10:00Z"
    assert result["live_overlay"] is True
    assert result["live_overlay_count"] == 1
    assert result["live_overlay_enabled"] is True
    assert result["live_overlay_after"] == "2026-05-22T07:55:00Z"
    assert result["live_overlay_limit"] == 10
    assert result["live_overlay_per_scope_limit"] == 10
    assert result["live_overlay_timeout_ms"] == 1500
    assert result["live_overlay_attempted"] is True
    assert result["live_overlay_latest_fetched_at"] == "2026-05-24T02:10:00Z"
    assert conn.commits >= 1
    assert "SET LOCAL idle_in_transaction_session_timeout = '5000ms'" in conn.sqls

    overlay_sql = next(sql for sql in conn.sqls if "FROM remote_poc.items i" in sql)
    assert "i.fetched_at > %(overlay_after)s::timestamptz" in overlay_sql
    assert "WITH recent AS MATERIALIZED" in overlay_sql
    assert "PARTITION BY recent.section_category" in overlay_sql
    assert "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC" in overlay_sql
    assert "LIMIT %(overlay_limit)s" in overlay_sql
    assert "overlay_rn <= %(overlay_per_scope_limit)s" in overlay_sql
    assert not any("GROUP BY" in sql for sql in conn.sqls if "FROM remote_poc.items i" in sql)

    sql_count_after_first_call = len(conn.sqls)
    cached_probe = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert cached_probe["live_overlay_enabled"] is True
    assert cached_probe["sections"]["products"][0]["id"] == "prod_recent"
    assert len(conn.sqls) == sql_count_after_first_call

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_search_uses_info_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_search_v1"
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '8000ms'"
    assert result["sections"]["products"][0]["id"] == "prod_search_1"
    assert result["cat_counts"] == {"products": 2}
    assert result["total"] == 2
    search_sql = next(sql for sql in conn.sqls if "PARTITION BY mc.category" in sql and "search_like" in sql)
    assert "matched_cards AS MATERIALIZED" in search_sql
    assert "ci.search_text ILIKE %(search_like)s" in search_sql
    assert "info_scope_items" not in search_sql
    assert "ORDER BY mc.sort_at DESC NULLS LAST" in search_sql
    assert "pr.card_json" in search_sql
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_search_does_not_fall_back_to_live_items_when_read_model_times_out(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelTimeoutConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "info_search_read_model_unavailable"
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_search_degraded_result_is_not_cached(monkeypatch):
    remote_db.clear_feed_cache_keys()
    timeout_conn = _InfoSearchReadModelTimeoutConn()
    ok_conn = _InfoSearchReadModelConn()
    current = {"conn": timeout_conn}

    @contextmanager
    def fake_connect():
        yield current["conn"]

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    degraded = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert degraded["degraded"] is True

    current["conn"] = ok_conn
    recovered = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert recovered["read_model"] == "info_search_v1"
    assert recovered["sections"]["products"][0]["id"] == "prod_search_1"

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_logged_in_uses_read_model_and_overlays_status(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelUserConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )

    first = result["sections"]["products"][0]
    assert result["read_model"] == "info_platforms_v1"
    assert first["id"] == "prod_recent"
    assert first["clicked_at"] == "2026-05-23T01:02:03Z"
    assert first["starred_at"] == "2026-05-23T01:03:04Z"
    assert any("FROM remote_poc.item_status" in sql for sql in conn.sqls)
    assert not any(
        "FROM remote_poc.items i" in sql and "i.platform = 'manual'" not in sql
        for sql in conn.sqls
    )

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_logged_in_merges_private_manual_overlay(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelManualOverlayConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )

    assert result["read_model"] == "info_platforms_v1"
    assert result["private_manual_overlay"] is True
    assert result["cat_counts"]["products"] == 3
    assert result["total"] == 3
    assert [item["id"] for item in result["sections"]["products"]] == [
        "prod_older",
        "manual_private",
        "prod_recent",
    ]
    assert any("i.platform = 'manual'" in sql for sql in conn.sqls)
    assert any((params or {}).get("manual_owner_user_id") == "user-1" for params in conn.params)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_logged_in_pages_private_manual_as_union(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelManualOverlayConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    first_page = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=2,
        keyword=None,
        search=None,
        subcategory=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )

    assert first_page["read_model"] == "info_platforms_v1"
    assert first_page["private_manual_overlay_page"] is True
    assert first_page["total"] == 81
    assert [item["id"] for item in first_page["items"]] == ["manual_private", "prod_rm_1"]

    second_page = remote_db.query_feed_by_category(
        category="products",
        offset=1,
        limit=1,
        keyword=None,
        search=None,
        subcategory=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )

    assert [item["id"] for item in second_page["items"]] == ["prod_rm_1"]
    assert second_page["offset"] == 1
    assert second_page["next_cursor"] is None
    assert any("i.platform = 'manual'" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_uses_section_read_model_for_more(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=50,
        limit=50,
        keyword=None,
        search=None,
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_platforms_v1"
    assert result["scope_dimension"] == "section_category"
    assert result["total"] == 80
    assert result["offset"] == 50
    assert [item["id"] for item in result["items"]] == ["prod_rm_51"]
    assert result["next_cursor"] == {
        "version_id": conn.version,
        "scope_key": "platform=_all|dimension=section_category|value=products",
        "rank_after": 51,
    }
    assert any("sc.dimension = %(scope_dimension)s" in sql for sql in conn.sqls)
    assert not any("si.rank > %(offset)s" in sql for sql in conn.sqls)
    assert not any("si.rank <= %(end_rank)s" in sql for sql in conn.sqls)
    assert any("ORDER BY si.sort_at DESC NULLS LAST" in sql for sql in conn.sqls)
    assert any("ORDER BY pr.sort_at DESC NULLS LAST" in sql for sql in conn.sqls)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_search_uses_section_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search="claude",
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_search_v1"
    assert result["scope_dimension"] == "section_category"
    assert result["total"] == 2
    assert result["items"][0]["id"] == "prod_search_page"
    page_sql = next(sql for sql in conn.sqls if "summary.scope_count" in sql and "search_like" in sql)
    pre_final_sql = page_sql.split(")\n                     SELECT av.version_id")[0]
    assert "ci.search_text ILIKE %(search_like)s" in page_sql
    assert "ci.card_json" not in pre_final_sql
    assert "ORDER BY si.sort_at DESC NULLS LAST" in page_sql
    assert "page_ci.card_json" in page_sql
    assert any("scope_key = %(scope_key)s" in sql for sql in conn.sqls)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_read_model_cursor_pins_version_and_rank(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        cursor={
            "version_id": "cursor-version-1",
            "scope_key": "platform=_all|dimension=section_category|value=products",
            "rank_after": 51,
        },
        keyword=None,
        search=None,
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["offset"] == 51
    assert [item["id"] for item in result["items"]] == ["prod_rm_52"]
    page_params = next(params for params in conn.params if (params or {}).get("cursor_version_id") == "cursor-version-1")
    assert page_params["offset"] == 51
    assert page_params["end_rank"] == 101

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_user_overlay_does_not_pollute_public_read_model_cache(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelUserConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    public_first = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search=None,
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert public_first["items"][0]["id"] == "prod_rm_1"
    assert public_first["items"][0].get("clicked_at") in (None, "")

    user_page = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search=None,
        subcategory=None,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id="user-1",
        min_github_stars=50,
    )
    assert user_page["items"][0]["id"] == "prod_rm_1"
    assert user_page["items"][0]["clicked_at"] == "2026-05-23T02:02:03Z"

    public_again = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search=None,
        subcategory=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert public_again["items"][0]["id"] == "prod_rm_1"
    assert public_again["items"][0].get("clicked_at") in (None, "")

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_subcategory_uses_section_subcategory_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search=None,
        subcategory="chatbot",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_platforms_v1"
    assert result["scope_dimension"] == "section_subcategory"
    assert result["scope_value"] == "products::chatbot"
    assert result["scope_key"] == "platform=_all|dimension=section_subcategory|value=products::chatbot"
    assert result["total"] == 80
    assert [item["id"] for item in result["items"]] == ["prod_rm_1"]
    assert any("section_subcategory" in (params or {}).get("scope_key", "") for params in conn.params)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_missing_read_model_scope_falls_back_live(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelMissingScopeConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        keyword=None,
        search=None,
        subcategory="chatbot",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert "read_model" not in result
    assert result["total"] == 682
    assert [item["id"] for item in result["items"]] == ["prod_live_1"]
    assert any("scope_count" in sql for sql in conn.sqls)
    assert any("SELECT count(*) AS n FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_reuses_canonical_section_cache_for_aliases(monkeypatch):
    remote_db.clear_feed_cache_keys()
    cache_key = remote_db._info_read_model_section_category_page_cache_key(
        schema="remote_poc",
        category="efficiency_tools",
        offset=50,
        limit=50,
    )
    remote_db._cache_set_copy(
        cache_key,
        {
            "items": [_row("tool_cached", category="efficiency_tools")],
            "category": "efficiency_tools",
            "total": 6093,
            "offset": 50,
            "limit": 50,
            "has_more": True,
            "next_offset": 100,
            "data_backend": "supabase_poc",
            "read_model": "info_platforms_v1",
            "scope_dimension": "section_category",
            "scope_value": "efficiency_tools",
        },
    )

    @contextmanager
    def fail_connect():
        raise AssertionError("canonical alias cache should avoid remote db")
        yield

    monkeypatch.setattr(remote_db, "connect", fail_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_category(
        category="efficiency_tools",
        offset=50,
        limit=50,
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["tool_cached"]
    assert result["read_model"] == "info_platforms_v1"

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_group_source_uses_compound_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_platform(
        platform="lingowhale",
        offset=0,
        limit=50,
        source="AI产品-公众号",
        group="AI周刊",
        public_only=True,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_platforms_v1"
    assert result["scope_dimension"] == "group_source"
    assert result["scope_value"] == "AI周刊::AI产品-公众号"
    assert result["scope_key"] == "platform=lingowhale|dimension=group_source|value=AI周刊::AI产品-公众号"
    assert result["items"][0]["id"] == "tw_rm_51"
    assert any("group_source" in (params or {}).get("scope_key", "") for params in conn.params)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_search_uses_scope_read_model(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoSearchReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        search="claude",
        exclude_ids=["tw_search_1"],
        public_only=True,
        min_github_stars=50,
    )

    assert result["read_model"] == "info_search_v1"
    assert result["scope_dimension"] == "source"
    assert result["scope_key"] == "platform=twitter|dimension=source|value=following"
    assert result["total"] == 2
    assert result["items"][0]["id"] == "tw_search_page"
    page_sql = next(sql for sql in conn.sqls if "summary.scope_count" in sql and "platform=twitter" not in sql)
    pre_final_sql = page_sql.split(")\n                     SELECT av.version_id")[0]
    assert "ci.search_text ILIKE %(search_like)s" in page_sql
    assert "ci.card_json" not in pre_final_sql
    assert "ORDER BY si.sort_at DESC NULLS LAST" in page_sql
    assert "page_ci.card_json" in page_sql
    assert "AND NOT (si.item_id = ANY(%(exclude_ids)s))" in page_sql
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_returns_pagination_total(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_51"), _row("tw_52")], count=75)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=50,
        limit=2,
        source="following",
        category="ai",
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["tw_51", "tw_52"]
    assert result["total"] == 75
    assert result["offset"] == 50
    assert result["limit"] == 2
    assert result["has_more"] is True
    assert result["next_offset"] == 52
    assert result["data_backend"] == "supabase_poc"

    count_sql = next(sql for sql in conn.sqls if "SELECT count(*) AS n" in sql)
    assert "i.platform = %(platform)s" in count_sql
    assert "i.source = %(source)s" in count_sql
    assert "i.ai_category IS NOT NULL" in count_sql
    assert "i.visible = 1" in count_sql
    assert "COALESCE(i.source, '') != 'bookmarks'" in count_sql
    assert any(params and params.get("source") == "following" for params in conn.params)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_uses_info_read_model_for_pill_page(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=50,
        limit=50,
        source="following",
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["tw_rm_51"]
    assert result["total"] == 80
    assert result["offset"] == 50
    assert result["limit"] == 50
    assert result["has_more"] is True
    assert result["next_offset"] == 51
    assert result["next_cursor"] == {
        "version_id": conn.version,
        "scope_key": "platform=twitter|dimension=source|value=following",
        "rank_after": 51,
    }
    assert result["data_backend"] == "supabase_poc"
    assert any("scope_key" in (params or {}) for params in conn.params)
    page_sql = next(sql for sql in conn.sqls if "WITH active_version AS" in sql and "page_rows AS" in sql)
    assert "COALESCE(ci.source, '') != 'bookmarks'" in page_sql
    assert any("info_scope_items" in sql for sql in conn.sqls)
    assert not any("FROM remote_poc.items i" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_read_model_cursor_pins_version_and_rank(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        cursor={
            "version_id": "cursor-version-2",
            "scope_key": "platform=twitter|dimension=source|value=following",
            "rank_after": 51,
        },
        public_only=True,
        min_github_stars=50,
    )

    assert result["offset"] == 51
    page_params = next(params for params in conn.params if (params or {}).get("cursor_version_id") == "cursor-version-2")
    assert page_params["offset"] == 51
    assert page_params["scope_key"] == "platform=twitter|dimension=source|value=following"

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_reuses_cached_info_read_model_page(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    first = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        public_only=True,
        min_github_stars=50,
    )
    sql_count_after_first = len(conn.sqls)
    second = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        public_only=True,
        min_github_stars=50,
    )

    assert first["items"][0]["id"] == "tw_rm_51"
    assert second["items"][0]["id"] == "tw_rm_51"
    assert len(conn.sqls) == sql_count_after_first

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_read_model_uses_loaded_count_as_rank_offset(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        exclude_ids=[f"tw_{idx}" for idx in range(1, 51)],
        public_only=True,
        min_github_stars=50,
    )

    item_sql = next(
        sql for sql in conn.sqls
        if "FROM remote_poc.info_scope_items si" in sql
        and "JOIN remote_poc.info_card_items" in sql
    )
    item_params = conn.params[-1]
    assert result["offset"] == 50
    assert result["next_offset"] == 51
    assert item_params["offset"] == 50
    assert item_params["exclude_ids"] == []
    assert "NOT (si.item_id = ANY(%(exclude_ids)s))" not in item_sql

    remote_db.clear_feed_cache_keys()


def test_prewarm_info_read_model_pages_populates_hot_scope_cache(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelPrewarmConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.prewarm_info_read_model_pages(max_scopes=2, page_limit=50)

    assert result["ok"] is True
    assert result["pages"] == 2
    assert result["items"] == 4
    assert any(
        "PARTITION BY CASE" in sql and "WHEN dimension IN ('section_category', 'section_subcategory')" in sql
        for sql in conn.sqls
    )

    @contextmanager
    def fail_connect():
        raise AssertionError("cached hot page should not touch remote db")
        yield

    monkeypatch.setattr(remote_db, "connect", fail_connect)
    page = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in page["items"]] == ["tw_hot_1", "tw_hot_2"]
    assert page["total"] == 80
    assert page["read_model"] == "info_platforms_v1"

    next_page = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        source="following",
        exclude_ids=[f"tw_hot_{idx}" for idx in range(1, 51)],
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in next_page["items"]] == ["tw_hot_51", "tw_hot_52"]
    assert next_page["offset"] == 50
    assert next_page["read_model"] == "info_platforms_v1"

    remote_db.clear_feed_cache_keys()


def test_refresh_info_read_model_builds_version_and_swaps_active(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelBuildConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db.uuid, "uuid4", lambda: "00000000-0000-0000-0000-000000000002")

    result = remote_db.refresh_info_read_model(sample_limit=150, min_github_stars=50)

    joined_sql = "\n".join(conn.sqls)
    assert result["ok"] is True
    assert result["version_id"] == "00000000-0000-0000-0000-000000000002"
    assert result["card_items"] == 123
    assert result["scope_items"] == 321
    assert "timings_ms" in result
    assert "INSERT INTO remote_poc.info_read_model_versions" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_eligible" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_scope_rows" in joined_sql
    assert "INSERT INTO remote_poc.info_card_items" in joined_sql
    assert "INSERT INTO remote_poc.info_scopes" in joined_sql
    assert "INSERT INTO remote_poc.info_scope_items" in joined_sql
    assert "SELECT i.*, COALESCE" not in joined_sql
    assert "'_all'::text AS platform, 'section_category'::text AS dimension" in joined_sql
    assert "'_all'::text AS platform, 'section_subcategory'::text AS dimension" in joined_sql
    assert "'group_source'::text AS dimension" in joined_sql
    assert "sort_at AS rank_at" in joined_sql
    assert "WHEN dimension IN ('section_category', 'section_subcategory') THEN fetched_at" not in joined_sql
    assert "rank_at DESC NULLS LAST" in joined_sql
    assert "INSERT INTO remote_poc.info_read_model_state" in joined_sql
    assert "ON CONFLICT (key) DO UPDATE" in joined_sql
    assert "DELETE FROM remote_poc.info_read_model_versions" in joined_sql
    assert "FROM remote_poc.info_read_model_state" in joined_sql
    assert "active_version_id" in joined_sql
    assert conn.commits == 2
    assert conn.rollbacks == 0

    remote_db.clear_feed_cache_keys()


def test_prune_info_read_model_versions_protects_active_state():
    conn = _InfoReadModelBuildConn()

    remote_db._prune_info_read_model_versions(conn, schema="remote_poc")

    joined_sql = "\n".join(conn.sqls)
    assert "DELETE FROM remote_poc.info_read_model_versions" in joined_sql
    assert "FROM remote_poc.info_read_model_state" in joined_sql
    assert "active_version_id" in joined_sql
    assert "status = 'complete'" in joined_sql
    assert "generated_at < now() - interval '6 hours'" in joined_sql


def test_refresh_info_read_model_incremental_clones_active_and_reranks_delta_scopes(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelIncrementalConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db.uuid, "uuid4", lambda: "00000000-0000-0000-0000-000000000003")

    result = remote_db.refresh_info_read_model_incremental(sample_limit=150, min_github_stars=50)

    joined_sql = "\n".join(conn.sqls)
    assert result["ok"] is True
    assert result["mode"] == "incremental"
    assert result["version_id"] == "00000000-0000-0000-0000-000000000003"
    assert result["parent_version_id"] == conn.active_version
    assert result["delta_items"] == 2
    assert result["card_items"] == 125
    assert result["scope_items"] == 330
    assert "CREATE TEMP TABLE info_read_model_delta" in joined_sql
    assert "i.fetched_at > %(active_max_fetched_at)s::timestamptz" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_delta_scope_rows" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_affected_scopes" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_affected_scope_rows" in joined_sql
    assert "FROM remote_poc.info_card_items ci" in joined_sql
    assert "NOT EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes" in joined_sql
    assert "PARTITION BY scope_key, item_id" in joined_sql
    assert "sort_at AS rank_at" in joined_sql
    assert "WHEN sc.dimension IN ('section_category', 'section_subcategory') THEN si.fetched_at" not in joined_sql
    assert "rank_at DESC NULLS LAST" in joined_sql
    assert "INSERT INTO remote_poc.info_read_model_state" in joined_sql
    assert "DELETE FROM remote_poc.info_read_model_versions" in joined_sql
    assert "FROM remote_poc.info_read_model_state" in joined_sql
    assert "'mode', 'incremental'" in joined_sql
    assert conn.rollbacks == 0

    remote_db.clear_feed_cache_keys()


def test_refresh_info_read_model_delta_in_place_upserts_delta_and_reranks_scopes(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelIncrementalConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)

    result = remote_db.refresh_info_read_model_delta_in_place(sample_limit=150, min_github_stars=50)
    joined_sql = "\n".join(conn.sqls)

    assert result["ok"] is True
    assert result["mode"] == "delta_in_place"
    assert result["version_id"] == conn.active_version
    assert result["delta_items"] == 2
    assert "ON CONFLICT (version_id, item_id) DO UPDATE SET" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_existing_delta_scope_rows" in joined_sql
    assert "DELETE FROM remote_poc.info_scope_items si" in joined_sql
    assert "UPDATE remote_poc.info_scope_items si" in joined_sql
    assert "INSERT INTO remote_poc.info_scope_items" in joined_sql
    assert "scope_max_rank" in joined_sql
    assert "WITH deduped AS" not in joined_sql
    assert (
        "DELETE FROM remote_poc.info_scope_items si\n"
        "                      USING pg_temp.info_read_model_affected_scopes"
        not in joined_sql
    )
    assert "CREATE TEMP TABLE info_read_model_affected_scope_rows" not in joined_sql
    assert "'last_delta_mode', 'in_place'" in joined_sql
    assert "SELECT %(version_id)s::uuid, ci.item_id" not in joined_sql
    assert "DELETE FROM remote_poc.info_read_model_versions" in joined_sql
    assert "FROM remote_poc.info_read_model_state" in joined_sql

    remote_db.clear_feed_cache_keys()


def test_refresh_info_read_model_if_stale_skips_when_data_fresh(monkeypatch):
    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
    conn = _InfoReadModelFreshnessConn(
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T01:00:00+00:00",
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model",
        lambda **_kwargs: pytest.fail("fresh read model must not rebuild"),
    )

    result = remote_db.refresh_info_read_model_if_stale(min_interval_sec=0)

    assert result["ok"] is True
    assert result["skipped"] == "data_fresh"
    assert result["active_max_fetched_at"] == "2026-05-24T01:00:00Z"
    assert result["latest_max_fetched_at"] == "2026-05-24T01:00:00Z"

    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0


def test_info_read_model_freshness_remote_reports_active_and_latest(monkeypatch):
    conn = _InfoReadModelFreshnessConn(
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T02:00:00+00:00",
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db, "_info_read_model_incremental_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db, "_info_live_overlay_enabled", lambda *_args, **_kwargs: False)

    result = remote_db.info_read_model_freshness_remote(min_github_stars=50)

    assert result["enabled"] is True
    assert result["read_model"] == "info_platforms_v1"
    assert result["data_backend"] == "supabase_poc"
    assert result["incremental_enabled"] is True
    assert result["live_overlay_enabled"] is False
    assert result["active_generated_at"] == "2026-05-24T00:00:00Z"
    assert result["active_max_fetched_at"] == "2026-05-24T01:00:00Z"
    assert result["latest_max_fetched_at"] == "2026-05-24T02:00:00Z"
    assert result["sort_policy"] == remote_db.INFO_READ_MODEL_SORT_POLICY
    assert result["active_sort_policy"] == remote_db.INFO_READ_MODEL_SORT_POLICY
    assert result["sort_policy_stale"] is False
    assert result["stale"] is True
    assert any("ORDER BY i.fetched_at DESC NULLS LAST" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_migrate_info_read_model_sort_policy_reranks_active_version(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelSortPolicyMigrationConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db, "load_project_env", lambda _base: {})

    result = remote_db.migrate_info_read_model_sort_policy()
    joined_sql = "\n".join(conn.sqls)

    assert result["ok"] is True
    assert result["mode"] == "sort_policy_migration"
    assert result["version_id"] == conn.active_version
    assert result["scope_items"] == 330
    assert "UPDATE remote_poc.info_card_items" in joined_sql
    assert "CREATE TEMP TABLE info_read_model_reranked_scope_items" in joined_sql
    assert "ORDER BY sort_at DESC NULLS LAST" in joined_sql
    assert "DELETE FROM remote_poc.info_scope_items" in joined_sql
    assert "sort_policy_migration" in joined_sql
    assert conn.commits == 1

    remote_db.clear_feed_cache_keys()


def test_refresh_info_read_model_if_stale_migrates_when_sort_policy_changes(monkeypatch):
    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
    conn = _InfoReadModelFreshnessConn(
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T01:00:00+00:00",
        active_sort_policy="fetched_at_desc_legacy",
    )
    migration_calls = []

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "migrate_info_read_model_sort_policy",
        lambda **kwargs: migration_calls.append(kwargs) or {"ok": True, "mode": "sort_policy_migration", "version_id": "active-version"},
    )
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model",
        lambda **_kwargs: pytest.fail("sort policy migration should not rebuild card_json"),
    )
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model_delta_in_place",
        lambda **_kwargs: pytest.fail("data-fresh sort policy migration should not apply delta"),
    )

    result = remote_db.refresh_info_read_model_if_stale(min_interval_sec=0)

    assert result == {"ok": True, "mode": "sort_policy_migration", "version_id": "active-version"}
    assert migration_calls == [{}]

    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0


def test_refresh_info_read_model_if_stale_migrates_then_applies_delta_when_data_is_stale(monkeypatch):
    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
    conn = _InfoReadModelFreshnessConn(
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T02:00:00+00:00",
        active_sort_policy="fetched_at_desc_legacy",
    )
    migration_calls = []
    delta_calls = []

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db, "_info_read_model_incremental_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "migrate_info_read_model_sort_policy",
        lambda **kwargs: migration_calls.append(kwargs) or {"ok": True, "mode": "sort_policy_migration", "version_id": "active-version"},
    )
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model_delta_in_place",
        lambda **kwargs: delta_calls.append(kwargs) or {"ok": True, "mode": "delta_in_place", "version_id": "active-version", "delta_items": 2},
    )

    result = remote_db.refresh_info_read_model_if_stale(min_interval_sec=0)

    assert result == {
        "ok": True,
        "mode": "delta_in_place",
        "version_id": "active-version",
        "delta_items": 2,
        "sort_policy_migration": {"ok": True, "mode": "sort_policy_migration", "version_id": "active-version"},
    }
    assert migration_calls == [{}]
    assert delta_calls == [{}]

    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0


def test_refresh_info_read_model_if_stale_applies_delta_when_latest_item_newer(monkeypatch):
    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0
    conn = _InfoReadModelFreshnessConn(
        active_max="2026-05-24T01:00:00+00:00",
        latest_max="2026-05-24T02:00:00+00:00",
    )
    delta_calls = []

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model_delta_in_place",
        lambda **kwargs: delta_calls.append(kwargs) or {"ok": True, "mode": "delta_in_place", "version_id": "active-version", "delta_items": 2},
    )

    result = remote_db.refresh_info_read_model_if_stale(min_interval_sec=0)

    assert result == {"ok": True, "mode": "delta_in_place", "version_id": "active-version", "delta_items": 2}
    assert delta_calls == [{}]

    remote_db.clear_feed_cache_keys()
    remote_db._INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = 0.0


def test_refresh_info_read_model_uses_configurable_build_timeout(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _InfoReadModelBuildConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(remote_db, "load_project_env", lambda _base: {})
    monkeypatch.setenv(remote_db.INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV, "240000")

    remote_db.refresh_info_read_model(sample_limit=150, min_github_stars=50)

    assert conn.sqls[0] == "SET LOCAL statement_timeout = '240000ms'"

    remote_db.clear_feed_cache_keys()


def test_prewarm_platforms_can_refresh_info_read_model_before_query(monkeypatch):
    refresh_calls = []
    section_query_calls = []
    query_calls = []
    page_prewarm_calls = []

    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        remote_db,
        "refresh_info_read_model_if_stale",
        lambda **kwargs: refresh_calls.append(kwargs) or {"ok": True, "scope_items": 321},
    )
    monkeypatch.setattr(
        remote_db,
        "query_feed_sections",
        lambda **kwargs: section_query_calls.append(kwargs) or {"sections": {}, "cat_counts": {}},
    )
    monkeypatch.setattr(
        remote_db,
        "query_feed_platforms",
        lambda **kwargs: query_calls.append(kwargs) or {"sections": {}, "platform_counts": {}, "source_counts": {}},
    )
    monkeypatch.setattr(
        remote_db,
        "prewarm_info_read_model_pages",
        lambda **kwargs: page_prewarm_calls.append(kwargs) or {"ok": True, "pages": 2, "items": 100},
    )

    result = remote_db.prewarm_platforms(
        refresh_mv=False,
        refresh_read_model=True,
        refresh_read_model_min_interval_sec=0,
    )

    assert refresh_calls == [{"min_interval_sec": 0}]
    assert section_query_calls and section_query_calls[0]["per_category"] == 50
    assert section_query_calls[0]["public_only"] is True
    assert query_calls and query_calls[0]["per_platform"] == 50
    assert page_prewarm_calls == [{"max_scopes": 2}]
    assert result["read_model_refresh_ok"] is True
    assert result["sections_query_ok"] is True
    assert result["read_model_page_prewarm_ok"] is True


def test_prewarm_platforms_can_refresh_highlights_read_model_before_query(monkeypatch):
    refresh_calls = []
    section_query_calls = []
    query_calls = []

    monkeypatch.setattr(remote_db, "_info_read_model_enabled", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        remote_db,
        "refresh_highlights_read_model_if_stale",
        lambda **kwargs: refresh_calls.append(kwargs) or {"ok": True, "scope_items": 654},
    )
    monkeypatch.setattr(
        remote_db,
        "query_feed_sections",
        lambda **kwargs: section_query_calls.append(kwargs) or {"sections": {}, "cat_counts": {}},
    )
    monkeypatch.setattr(
        remote_db,
        "query_feed_platforms",
        lambda **kwargs: query_calls.append(kwargs) or {"sections": {}, "platform_counts": {}, "source_counts": {}},
    )

    result = remote_db.prewarm_platforms(
        refresh_mv=False,
        refresh_read_model=False,
        refresh_highlights_read_model=True,
        refresh_highlights_read_model_min_interval_sec=0,
    )

    assert refresh_calls == [{"min_interval_sec": 0}]
    assert section_query_calls and section_query_calls[0]["per_category"] == 50
    assert query_calls and query_calls[0]["per_platform"] == 50
    assert result["highlights_read_model_refresh_ok"] is True
    assert result["highlights_read_model_scope_items"] == 654


def test_query_feed_by_platform_uses_lean_card_columns(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_1")], count=1)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        source="following",
        public_only=True,
        min_github_stars=50,
    )

    assert result["items"][0]["id"] == "tw_1"
    item_sql = next(
        sql for sql in conn.sqls
        if "FROM remote_poc.items i" in sql and "SELECT count(*) AS n" not in sql
    )
    assert "NULL::jsonb AS detail_json" in item_sql
    assert "NULL::jsonb AS comments_json" in item_sql
    assert "NULL::jsonb AS tags_json" in item_sql
    assert "NULL::text AS ai_key_points" in item_sql
    assert "NULL::text AS ai_keywords" in item_sql
    assert "NULL::jsonb AS ai_subcategories" in item_sql
    assert "NULL::jsonb AS ai_extracted" in item_sql
    assert "NULL::text AS multi_l1_reason" in item_sql
    assert "i.ai_key_points" not in item_sql
    assert "i.ai_keywords" not in item_sql
    assert "i.ai_extracted" not in item_sql

    remote_db.clear_feed_cache_keys()


def test_get_feed_item_keeps_detail_columns(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_1")], count=1)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.get_feed_item(item_id="tw_1", public_only=True)

    assert result["id"] == "tw_1"
    item_sql = next(sql for sql in conn.sqls if "FROM remote_poc.items i" in sql)
    assert "i.content" in item_sql
    assert "i.detail_json" in item_sql
    assert "i.comments_json" in item_sql
    assert "i.ai_key_points" in item_sql
    assert "i.ai_keywords" in item_sql
    assert "i.ai_extracted" in item_sql

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_warms_platform_more_count_cache(monkeypatch):
    remote_db.clear_feed_cache_keys()
    min_github_stars = 9876
    overview_conn = _PlatformOverviewConn(
        item_rows=[_row("tw_1")],
        platform_rows=[{"platform": "twitter", "cnt": 120}],
        source_rows=[{"platform": "twitter", "source": "following", "cnt": 80}],
        category_rows=[{"platform": "twitter", "category": "ai", "cnt": 90}],
    )
    page_conn = _QueryFeedConn(rows=[_row("tw_51"), _row("tw_52")], count=999, fail_count=True)
    conns = [overview_conn, page_conn]

    @contextmanager
    def fake_connect():
        yield conns.pop(0)

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_platforms_mv_available", lambda _conn, _schema: True)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    remote_db.query_feed_platforms(
        per_platform=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=min_github_stars,
    )
    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=50,
        limit=2,
        public_only=True,
        min_github_stars=min_github_stars,
    )

    assert [item["id"] for item in result["items"]] == ["tw_51", "tw_52"]
    assert result["total"] == 120
    assert result["has_more"] is True
    assert not any("SELECT count(*) AS n" in sql for sql in page_conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_count_timeout_returns_items_with_estimated_total(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_51"), _row("tw_52")], fail_count=True)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=50,
        limit=2,
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["tw_51", "tw_52"]
    assert result["total"] == 53
    assert result["has_more"] is True
    assert result["next_offset"] == 52
    assert result["degraded"] is True
    assert result["degraded_reason"] == "platform_page_total_unavailable"
    assert result["total_is_estimate"] is True

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_platform_excludes_loaded_ids_without_changing_total(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_53")], count=75)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_by_platform(
        platform="twitter",
        offset=0,
        limit=50,
        exclude_ids=["tw_1", "tw_2"],
        public_only=True,
        min_github_stars=50,
    )

    item_sql = next(
        sql for sql in conn.sqls
        if "FROM remote_poc.items i" in sql and "SELECT count(*) AS n" not in sql
    )
    count_sql = next(sql for sql in conn.sqls if "SELECT count(*) AS n" in sql)
    assert "NOT (i.id::text = ANY(%(exclude_ids)s))" in item_sql
    assert "exclude_ids" in conn.params[1]
    assert "exclude_ids" not in conn.params[-1]
    assert "NOT (i.id::text = ANY(%(exclude_ids)s))" not in count_sql
    assert result["total"] == 75

    remote_db.clear_feed_cache_keys()


def test_query_feed_uses_short_timeout_and_skips_heavy_json(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed(
        limit=30,
        offset=0,
        public_only=True,
        min_github_stars=50,
    )

    assert result["total"] == 123
    assert result["items"][0]["id"] == "tw_1"
    assert result["data_backend"] == "supabase_poc"
    assert conn.sqls.count("SET LOCAL statement_timeout = '2500ms'") == 2
    item_sql = next(
        sql for sql in conn.sqls
        if "FROM remote_poc.items i" in sql
        and "ORDER BY i.fetched_at" in sql
        and "count(*) AS n" not in sql
    )
    assert "NULL::jsonb AS detail_json" in item_sql
    assert "NULL::jsonb AS comments_json" in item_sql
    assert "i.detail_json" not in item_sql
    assert "i.comments_json" not in item_sql

    remote_db.clear_feed_cache_keys()


def test_query_feed_count_timeout_returns_items_with_estimated_total(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(rows=[_row("tw_1"), _row("tw_2")], fail_count=True)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed(
        limit=30,
        offset=0,
        public_only=True,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["tw_1", "tw_2"]
    assert result["total"] == 2
    assert result["degraded"] is True
    assert result["degraded_reason"] == "feed_total_unavailable"
    assert result["total_is_estimate"] is True

    remote_db.clear_feed_cache_keys()


def test_query_feed_item_timeout_uses_stale_local_cache(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _QueryFeedConn(fail_items=True)
    stale_payload = {
        "items": [{"id": "cached"}],
        "total": 1,
        "offset": 0,
        "limit": 30,
        "data_backend": "supabase_poc",
    }

    @contextmanager
    def fake_connect():
        yield conn

    def fake_read_local_cache(name, *, max_age_sec=None):
        if max_age_sec == remote_db._LOCAL_READ_CACHE_FRESH_SEC:
            return None
        return stale_payload

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", fake_read_local_cache)

    result = remote_db.query_feed(
        limit=30,
        offset=0,
        public_only=True,
        min_github_stars=50,
    )

    assert result["items"] == [{"id": "cached"}]
    assert result["degraded"] is True
    assert result["stale"] is True
    assert result["stale_source"] == "local_read_cache"

    remote_db.clear_feed_cache_keys()


def test_refresh_platforms_mv_skips_blocking_fallback_by_default(monkeypatch):
    conn = _FakeRefreshConn(fail_concurrent=True)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setenv(remote_db.ALLOW_BLOCKING_MV_REFRESH_ENV, "0")

    result = remote_db.refresh_platforms_mv()

    assert result["ok"] is False
    assert result["blocking_skipped"] is True
    assert conn.autocommit is False
    assert conn.rollbacks == 1
    assert conn.sqls == [
        "REFRESH MATERIALIZED VIEW CONCURRENTLY remote_poc.mv_items_top_per_platform"
    ]


def test_refresh_platforms_mv_uses_concurrent_autocommit(monkeypatch):
    conn = _FakeRefreshConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.refresh_platforms_mv()

    assert result["ok"] is True
    assert result["mode"] == "concurrent"
    assert conn.autocommit is False
    assert conn.sqls == [
        "REFRESH MATERIALIZED VIEW CONCURRENTLY remote_poc.mv_items_top_per_platform",
        "SELECT count(*) AS n FROM remote_poc.mv_items_top_per_platform",
    ]


def test_prewarm_platforms_default_skips_mv_and_uses_platform_query_params(monkeypatch):
    remote_db.clear_feed_cache_keys()
    calls = []

    def fake_refresh():
        calls.append("refresh")
        raise AssertionError("periodic prewarm must not refresh the platforms MV")

    def fake_query(**kwargs):
        calls.append(("query", kwargs))
        return {"sections": {}, "platform_counts": {}, "source_counts": {}}

    monkeypatch.setattr(remote_db, "refresh_platforms_mv", fake_refresh)
    monkeypatch.setattr(remote_db, "query_feed_platforms", fake_query)

    result = remote_db.prewarm_platforms()

    assert result["mv_refresh_skipped"] is True
    assert result["query_ok"] is True
    assert calls == [("query", {
        "per_platform": 50,
        "search": None,
        "user_id": None,
        "public_only": True,
        "manual_owner_user_id": None,
        "min_github_stars": 50,
    })]


def test_prewarm_platforms_can_refresh_mv_when_explicitly_requested(monkeypatch):
    remote_db.clear_feed_cache_keys()
    calls = []

    def fake_refresh():
        calls.append("refresh")
        return {"ok": True}

    def fake_query(**kwargs):
        calls.append("query")
        return {"sections": {}, "platform_counts": {}, "source_counts": {}}

    monkeypatch.setattr(remote_db, "refresh_platforms_mv", fake_refresh)
    monkeypatch.setattr(remote_db, "query_feed_platforms", fake_query)
    monkeypatch.setenv(remote_db.ALLOW_PLATFORM_MV_REFRESH_ENV, "1")

    result = remote_db.prewarm_platforms(refresh_mv=True)

    assert result["mv_refresh_ok"] is True
    assert result["query_ok"] is True
    assert calls == ["refresh", "query"]


def test_prewarm_platforms_requires_mv_refresh_allow_flag(monkeypatch):
    remote_db.clear_feed_cache_keys()
    calls = []

    def forbidden_refresh():
        calls.append("refresh")
        raise AssertionError("explicit prewarm MV refresh requires allow flag")

    def fake_query(**kwargs):
        calls.append("query")
        return {"sections": {}, "platform_counts": {}, "source_counts": {}}

    monkeypatch.setattr(remote_db, "refresh_platforms_mv", forbidden_refresh)
    monkeypatch.setattr(remote_db, "query_feed_platforms", fake_query)
    monkeypatch.delenv(remote_db.ALLOW_PLATFORM_MV_REFRESH_ENV, raising=False)

    result = remote_db.prewarm_platforms(refresh_mv=True)

    assert result["mv_refresh_ok"] is False
    assert result["mv_refresh_skipped"] is True
    assert result["mv_refresh_skipped_reason"] == "platform_mv_refresh_not_allowed"
    assert result["query_ok"] is True
    assert calls == ["query"]


def test_get_stats_public_anonymous_uses_platforms_fast_path(monkeypatch):
    remote_db.clear_feed_cache_keys()

    def fail_connect():
        raise AssertionError("/api/stats public anonymous path must not GROUP BY remote items")

    def fake_platforms(**kwargs):
        return {
            "platform_counts": {"twitter": 2, "github": 1},
            "sections": {},
            "source_counts": {},
        }

    monkeypatch.setattr(remote_db, "connect", fail_connect)
    monkeypatch.setattr(remote_db, "query_feed_platforms", fake_platforms)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.get_stats(user_id=None, public_only=True, manual_owner_user_id=None)

    assert result == {
        "twitter": {"total": 2, "unread": 2},
        "github": {"total": 1, "unread": 1},
    }


def test_fetch_events_default_degrades_on_remote_error(monkeypatch):
    remote_db.clear_feed_cache_keys()

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("pool checkout timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert result["degraded"] is True
    assert result["events"] == []
    assert result["next_cursor"] is None
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_fetch_events_degraded_result_is_not_cached(monkeypatch):
    remote_db.clear_feed_cache_keys()
    writes = []
    connect_attempts = {"n": 0}

    @contextmanager
    def fake_connect():
        connect_attempts["n"] += 1
        if connect_attempts["n"] == 1:
            raise remote_db.RemoteDBError("pool checkout timeout")

        class FakeCursor:
            def __init__(self, rows=None, row=None):
                self._rows = rows or []
                self._row = row

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._row

        class FakeConn:
            def execute(self, sql, params=None):
                normalized = " ".join(sql.split())
                if normalized.startswith("SET LOCAL"):
                    return FakeCursor()
                if "GROUP BY day" in normalized:
                    return FakeCursor(rows=[{"day": "2026-05-19", "n": 1}])
                if "SELECT count(*) AS n" in normalized:
                    return FakeCursor(row={"n": 1})
                if "GROUP BY day" in normalized:
                    return FakeCursor(rows=[{"day": "2026-05-19", "n": 1}])
                return FakeCursor(rows=[
                    {
                        "id": 101,
                        "ai_title": "Recovered event",
                        "ai_summary": "summary",
                        "doc_count": 2,
                        "unique_source_count": 2,
                        "first_doc_at": "2026-05-19T01:00:00+00:00",
                        "last_doc_at": "2026-05-19T01:00:00+00:00",
                        "platforms_json": ["rss"],
                        "cover_url": None,
                        "live_version": 1,
                        "last_updated_at": "2026-05-19T01:00:00+00:00",
                    }
                ])

        yield FakeConn()

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args: writes.append(args))
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *args, **kwargs: {})

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert result["degraded"] is True
    assert writes == []

    second = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert connect_attempts["n"] == 2
    assert second.get("degraded") is not True
    assert second["events"][0]["id"] == 101

    remote_db.clear_feed_cache_keys()


def test_fetch_events_remote_error_uses_stale_local_cache(monkeypatch):
    remote_db.clear_feed_cache_keys()
    stale_payload = {
        "enabled": True,
        "events": [{"id": 202, "title": "stale but visible"}],
        "next_cursor": None,
        "new_since_last_fetch": 0,
        "total_available_within_30d": 1,
        "data_backend": "supabase_poc",
    }

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("statement timeout")
        yield

    def fake_read_local_cache(name, *, max_age_sec=None):
        if max_age_sec == remote_db._LOCAL_READ_CACHE_FRESH_SEC:
            return None
        return stale_payload

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", fake_read_local_cache)

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert result["events"] == stale_payload["events"]
    assert result["degraded"] is True
    assert result["stale"] is True
    assert result["stale_source"] == "local_read_cache"

    remote_db.clear_feed_cache_keys()


def test_fetch_events_ignores_stale_local_cache_when_remote_is_healthy(monkeypatch):
    remote_db.clear_feed_cache_keys()
    stale_payload = {
        "enabled": True,
        "events": [{"id": 203, "title": "stale first paint"}],
        "next_cursor": None,
        "new_since_last_fetch": 0,
        "total_available_within_30d": 1,
        "data_backend": "supabase_poc",
    }

    connect_attempts = {"n": 0}

    class FakeCursor:
        def __init__(self, rows=None, row=None):
            self._rows = rows or []
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class FakeConn:
        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return FakeCursor()
            if "SELECT count(*) AS n" in normalized:
                return FakeCursor(row={"n": 1})
            if "GROUP BY day" in normalized:
                return FakeCursor(rows=[{"day": "2026-05-20", "n": 1}])
            return FakeCursor(rows=[
                {
                    "id": 204,
                    "ai_title": "Fresh remote event",
                    "ai_summary": "summary",
                    "doc_count": 1,
                    "unique_source_count": 1,
                    "first_doc_at": "2026-05-20T01:00:00+00:00",
                    "last_doc_at": "2026-05-20T01:00:00+00:00",
                    "platforms_json": ["twitter"],
                    "cover_url": None,
                    "live_version": 1,
                    "last_updated_at": "2026-05-20T01:00:00+00:00",
                }
            ])

    @contextmanager
    def fake_connect():
        connect_attempts["n"] += 1
        yield FakeConn()

    def fake_read_local_cache(name, *, max_age_sec=None):
        if max_age_sec == remote_db._LOCAL_READ_CACHE_FRESH_SEC:
            return None
        return stale_payload

    writes = []
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")
    monkeypatch.setattr(remote_db, "_read_local_read_cache", fake_read_local_cache)
    monkeypatch.setattr(remote_db, "_write_local_read_cache_async", lambda *args: writes.append(args))
    monkeypatch.setattr(remote_db, "_write_feed_snapshot_async", lambda *args: None)
    monkeypatch.setattr(remote_db, "_read_feed_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_db, "_fetch_event_source_metadata", lambda *args, **kwargs: {})

    result = remote_db.fetch_events(
        page=1,
        limit=20,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert connect_attempts["n"] == 1
    assert result.get("stale") is not True
    assert result["events"][0]["id"] == 204
    assert writes

    remote_db.clear_feed_cache_keys()


def test_clear_feed_cache_keys_removes_feed_local_read_cache_files(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_db, "_LOCAL_READ_CACHE_DIR", tmp_path)
    feed_cache = remote_db._local_read_cache_path("feed_events_limit=20_public=1")
    feed_cache.parent.mkdir(parents=True, exist_ok=True)
    feed_cache.write_text(json.dumps({"events": [{"id": 1}]}), encoding="utf-8")
    auth_cache = remote_db._local_read_cache_path("auth_session_user_123")
    auth_cache.write_text(json.dumps({"user": 123}), encoding="utf-8")

    removed = remote_db.clear_feed_cache_keys()

    assert removed >= 0
    assert not feed_cache.exists()
    assert auth_cache.exists()


def test_prewarm_platforms_does_not_refresh_mv_by_default(monkeypatch):
    def forbidden_refresh():
        raise AssertionError("prewarm must not refresh materialized view by default")

    calls = []
    monkeypatch.setattr(remote_db, "refresh_platforms_mv", forbidden_refresh)
    monkeypatch.setattr(remote_db, "query_feed_platforms", lambda **kwargs: calls.append(kwargs) or {})

    result = remote_db.prewarm_platforms()

    assert result["mv_refresh_skipped"] is True
    assert result["query_ok"] is True
    assert calls == [
        {
            "per_platform": 50,
            "search": None,
            "user_id": None,
            "public_only": True,
            "manual_owner_user_id": None,
            "min_github_stars": 50,
        }
    ]


def test_local_read_cache_ignores_degraded_snapshots(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_db, "_read_local_read_cache", _ORIGINAL_READ_LOCAL_READ_CACHE)
    monkeypatch.setattr(remote_db, "_LOCAL_READ_CACHE_DIR", tmp_path)

    path = remote_db._local_read_cache_path("feed_events_limit=20")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"degraded": True, "events": []}), encoding="utf-8")

    assert remote_db._read_local_read_cache("feed_events_limit=20") is None


def test_query_feed_sections_degraded_result_is_not_cached(monkeypatch):
    remote_db.clear_feed_cache_keys()
    circuit = {"open": True}

    class SectionConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return _FakeResult([])
            if "count(*) AS cnt" in normalized:
                return _FakeResult([{"section_category": "products", "cnt": 1}])
            row = _row("section_live", category="products", ai_categories=["products"])
            row["section_category"] = "products"
            return _FakeResult([row])

    conn = SectionConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_remote_feed_live_circuit_open", lambda: circuit["open"])

    first = remote_db.query_feed_sections(
        per_category=50,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert first["degraded"] is True

    circuit["open"] = False
    second = remote_db.query_feed_sections(
        per_category=50,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert second.get("degraded") is not True
    assert second["sections"]["products"][0]["id"] == "section_live"

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_degraded_result_is_not_cached(monkeypatch):
    remote_db.clear_feed_cache_keys()
    circuit = {"open": True}
    conn = _PlatformOverviewConn(
        item_rows=[_row("tw_live", source="following", category="ai")],
        platform_rows=[{"platform": "twitter", "cnt": 1}],
        source_rows=[{"platform": "twitter", "source": "following", "cnt": 1}],
        category_rows=[{"platform": "twitter", "category": "ai", "cnt": 1}],
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "_remote_feed_live_circuit_open", lambda: circuit["open"])

    first = remote_db.query_feed_platforms(
        per_platform=50,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert first["degraded"] is True

    circuit["open"] = False
    second = remote_db.query_feed_platforms(
        per_platform=50,
        user_id="user-1",
        public_only=False,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    assert second.get("degraded") is not True
    assert second["sections"]["twitter"][0]["id"] == "tw_live"

    remote_db.clear_feed_cache_keys()


def test_pool_defaults_use_short_timeouts(monkeypatch):
    calls = {}

    class FakePool:
        def __init__(self, **kwargs):
            calls.update(kwargs)

    monkeypatch.setattr(remote_db, "_POOL", None)
    monkeypatch.setattr(remote_db, "_POOL_DSN", None)
    monkeypatch.setattr(remote_db, "_runtime_env", lambda: {})
    monkeypatch.setattr(remote_db, "database_url", lambda: "postgresql://example.test/db")
    monkeypatch.delenv(remote_db.REMOTE_DB_POOL_DISABLED_ENV, raising=False)
    monkeypatch.delenv(remote_db.REMOTE_DB_POOL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(remote_db.REMOTE_DB_CONNECT_TIMEOUT_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "psycopg_pool", types.SimpleNamespace(ConnectionPool=FakePool))

    pool = remote_db._get_pool(psycopg_module=None, dict_row="dict_row")

    assert isinstance(pool, FakePool)
    assert calls["timeout"] == 2.0
    assert calls["kwargs"]["connect_timeout"] == 2
    assert calls["kwargs"]["prepare_threshold"] is None


def test_fetch_events_small_limit_degrades_on_remote_error(monkeypatch):
    remote_db.clear_feed_cache_keys()

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("pool checkout timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "event_read_backend", lambda: "supabase_poc")

    result = remote_db.fetch_events(
        page=1,
        limit=3,
        user_id=None,
        public_only=True,
        min_github_stars=50,
        enabled=True,
    )

    assert result["degraded"] is True
    assert result["events"] == []
    assert result["next_cursor"] is None
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_has_recent_running_fetch_remote_checks_recent_running_rows(monkeypatch):
    conn = _FakeOneConn({"has_running": True})

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "fetch_run_heartbeat_grace_seconds", lambda: 600)

    assert remote_db.has_recent_running_fetch_remote(max_age_minutes=45) is True
    assert "status = 'running'" in conn.sqls[-1]
    assert "started_at >= now()" in conn.sqls[-1]
    assert "stats_json->>'_heartbeat_at'" in conn.sqls[-1]
    assert conn.params[-1] == (45, 600)


def test_query_feed_sections_live_path_uses_full_counts_not_sample_rows(monkeypatch):
    remote_db.clear_feed_cache_keys()
    min_github_stars = 9877
    conn = _PlatformOverviewConn(
        item_rows=[
            {**_row("tw_1", source="following", category="ai"), "section_category": "ai"},
            {**_row("tw_2", source="bookmarks", category="coding"), "section_category": "coding"},
            {**_row("gh_1", platform="github", source="trending", category="coding"), "section_category": "coding"},
        ],
        category_rows=[
            {"section_category": "ai", "cnt": 120},
            {"section_category": "coding", "cnt": 80},
        ],
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_platforms_mv_available", lambda _conn, _schema: True)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=min_github_stars,
    )

    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert not any("mv_items_top_per_platform" in sql for sql in conn.sqls)
    item_sql = next(sql for sql in conn.sqls if "WITH ranked AS" in sql)
    assert "WHERE rn <= %(per_category)s" in item_sql
    assert "FROM remote_poc.items i" in item_sql
    assert result["sections"]["ai"][0]["id"] == "tw_1"
    assert {item["id"] for item in result["sections"]["coding"]} == {"tw_2", "gh_1"}
    assert result["cat_counts"] == {"ai": 120, "coding": 80}
    assert result["total"] == 200
    assert result["sample_limit"] == 50
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_degrades_when_live_path_fails(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _FailingConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_platforms(
        per_platform=3,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["sections"] == {}
    assert result["platform_counts"] == {}
    assert result["data_backend"] == "supabase_poc"
    assert not any("mv_items_top_per_platform" in sql for sql in conn.sqls)
    assert any("FROM remote_poc.items" in sql for sql in conn.sqls)
    assert conn.rollbacks == 1

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_degrades_when_live_path_fails(monkeypatch):
    remote_db.clear_feed_cache_keys()
    conn = _FailingConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_sections(
        per_category=3,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["sections"] == {}
    assert result["total"] == 0
    assert result["cat_counts"] == {}
    assert result["data_backend"] == "supabase_poc"
    assert not any("mv_items_top_per_platform" in sql for sql in conn.sqls)
    assert any("FROM remote_poc.items" in sql for sql in conn.sqls)
    assert conn.rollbacks == 1

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_reuses_cached_result(monkeypatch):
    remote_db.clear_feed_cache_keys()
    min_github_stars = 9878
    conn = _PlatformOverviewConn(
        item_rows=[
            {**_row("tw_1", source="following", category="ai"), "section_category": "ai"},
            {**_row("gh_1", platform="github", source="trending", category="coding"), "section_category": "coding"},
        ],
        category_rows=[
            {"section_category": "ai", "cnt": 1},
            {"section_category": "coding", "cnt": 1},
        ],
    )

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "_platforms_mv_available", lambda _conn, _schema: True)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=min_github_stars,
    )
    conn.sqls.clear()

    result = remote_db.query_feed_sections(
        per_category=50,
        search=None,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=min_github_stars,
    )

    assert conn.sqls == []
    assert result["sections"]["ai"][0]["id"] == "tw_1"
    assert result["sections"]["coding"][0]["id"] == "gh_1"


def test_query_feed_platforms_live_path_sets_short_statement_timeout(monkeypatch):
    remote_db.clear_feed_cache_keys()

    class ScriptedConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return _FakeResult([])
            if "GROUP BY i.platform, i.source" in normalized:
                return _FakeResult([{"platform": "twitter", "source": "following", "cnt": 1}])
            if "GROUP BY i.platform, cat.value" in normalized:
                return _FakeResult([{"platform": "twitter", "category": "ai", "cnt": 1}])
            if "i.ai_categories IS NULL" in normalized:
                return _FakeResult([])
            if "GROUP BY i.platform" in normalized:
                return _FakeResult([{"platform": "twitter", "cnt": 1}])
            return _FakeResult([_row("tw_live")])

    conn = ScriptedConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["sections"]["twitter"][0]["id"] == "tw_live"
    assert result["platform_counts"] == {"twitter": 1}
    assert result["source_counts"] == {"twitter": {"following": 1}}
    assert result["category_counts"] == {"twitter": {"ai": 1}}
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert any("GROUP BY i.platform" in sql for sql in conn.sqls)
    assert any("GROUP BY i.platform, i.source" in sql for sql in conn.sqls)
    assert any("GROUP BY i.platform, cat.value" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_live_path_sets_short_statement_timeout(monkeypatch):
    remote_db.clear_feed_cache_keys()

    class ScriptedConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return _FakeResult([])
            if "AS section_category, count(*) AS cnt" in normalized:
                return _FakeResult([{"section_category": "ai", "cnt": 1}])
            return _FakeResult([dict(_row("section_live"), section_category="ai")])

    conn = ScriptedConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["sections"]["ai"][0]["id"] == "section_live"
    assert result["cat_counts"] == {"ai": 1}
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"
    assert len(conn.sqls) == 3
    assert any("GROUP BY 1" in sql for sql in conn.sqls)

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_sets_short_statement_timeout(monkeypatch):
    remote_db.clear_feed_cache_keys()

    class ScriptedConn:
        def __init__(self):
            self.sqls = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return _FakeResult([])
            if "SELECT count(*) AS n" in normalized:
                return _FakeRefreshResult({"n": 1})
            return _FakeResult([_row("category_live")])

    conn = ScriptedConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.query_feed_by_category(
        category="ai",
        offset=0,
        limit=25,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["items"][0]["id"] == "category_live"
    assert conn.sqls[0] == "SET LOCAL statement_timeout = '2500ms'"

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_matches_primary_section_scope_and_fetched_order(monkeypatch):
    remote_db.clear_feed_cache_keys()

    class ScriptedConn:
        def __init__(self):
            self.sqls = []
            self.params = []

        def execute(self, sql, params=None):
            self.sqls.append(sql)
            self.params.append(params or {})
            normalized = " ".join(sql.split())
            if normalized.startswith("SET LOCAL"):
                return _FakeResult([])
            if "SELECT count(*) AS n" in normalized:
                return _FakeRefreshResult({"n": 2})
            return _FakeResult([
                _row("prod_recent", category="products", ai_categories=["products", "tech"]),
                _row("prod_older", category="products", ai_categories=["products"]),
            ])

    conn = ScriptedConn()

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")

    result = remote_db.query_feed_by_category(
        category="products",
        offset=0,
        limit=50,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert [item["id"] for item in result["items"]] == ["prod_recent", "prod_older"]
    count_sql = next(sql for sql in conn.sqls if "SELECT count(*) AS n" in sql)
    item_sql = next(
        sql for sql in conn.sqls
        if "FROM remote_poc.items i" in sql and "SELECT count(*) AS n" not in sql
    )
    for sql in (count_sql, item_sql):
        assert "COALESCE(i.ai_categories ->> 0" in sql
        assert "= ANY(%(category_ids)s)" in sql
        assert "i.ai_category = ANY(%(category_ids)s)" not in sql
        assert "AND i.ai_categories IS NOT NULL" not in sql
    assert (
        "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
        "i.fetched_at DESC NULLS LAST, i.relevance_score DESC NULLS LAST, i.id DESC"
    ) in item_sql

    remote_db.clear_feed_cache_keys()


def test_query_feed_by_category_degrades_on_remote_error(monkeypatch):
    remote_db.clear_feed_cache_keys()

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("pool checkout timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_by_category(
        category="ai",
        offset=0,
        limit=25,
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result == {
        "items": [],
        "category": "ai",
        "total": 0,
        "data_backend": "supabase_poc",
        "degraded": True,
    }

    remote_db.clear_feed_cache_keys()


def test_query_feed_platforms_live_path_degrades_on_remote_error(monkeypatch):
    remote_db.clear_feed_cache_keys()

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("statement timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_platforms(
        per_platform=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["sections"] == {}
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_query_feed_sections_live_path_degrades_on_remote_error(monkeypatch):
    remote_db.clear_feed_cache_keys()

    @contextmanager
    def fake_connect():
        raise remote_db.RemoteDBError("statement timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    result = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert result["degraded"] is True
    assert result["sections"] == {}
    assert result["data_backend"] == "supabase_poc"

    remote_db.clear_feed_cache_keys()


def test_remote_feed_live_circuit_short_circuits_after_timeout(monkeypatch):
    remote_db.clear_feed_cache_keys()
    calls = {"n": 0}

    @contextmanager
    def fake_connect():
        calls["n"] += 1
        raise remote_db.RemoteDBError("statement timeout")
        yield

    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(remote_db, "feed_read_backend", lambda: "supabase_poc")

    first = remote_db.query_feed_platforms(
        per_platform=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    second = remote_db.query_feed_sections(
        per_category=50,
        search="claude",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )
    third = remote_db.query_feed_by_category(
        category="coding",
        user_id=None,
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=50,
    )

    assert first["degraded"] is True
    assert second["degraded"] is True
    assert third["degraded"] is True
    assert calls["n"] == 1

    remote_db.clear_feed_cache_keys()
