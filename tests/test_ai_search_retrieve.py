import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

INDEX_EXPRESSION = "(((((((COALESCE(i.title,'') || ' ') || COALESCE(i.author_name,'')) || ' ') || COALESCE(i.ai_summary,'')) || ' ') || COALESCE(i.ai_keywords,'')))"


def item(id, sim=None, hs10=None):
    return dict(id=id, title=id, ai_summary="summary", platform="rss",
                author_name="author", published_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
                url="https://example.test/" + id, cover_url=None, sim=sim, hs10=hs10,
                media_json=[], metrics_json={})


class FakeConnection:
    def __init__(self, lexical=(), vector=(), index=True, vector_error=None, index_error=None, lexical_error=None):
        self.lexical, self.vector, self.index = lexical, vector, index
        self.vector_error, self.index_error = vector_error, index_error
        self.lexical_error = lexical_error
        self.calls, self.rollbacks, self.checkouts = [], 0, 0
        self.rows = []
        self.aborted = False

    def execute(self, sql, params=None):
        assert not self.aborted, "transaction must be rolled back after SQL failure"
        self.calls.append((sql, params))
        if "pg_indexes" in sql:
            if self.index_error:
                self.aborted = True
                raise self.index_error
            self.rows = [{"available": self.index}]
        elif "&@~" in sql:
            if self.lexical_error:
                raise self.lexical_error
            self.rows = self.lexical
        elif "LIMIT 300" in sql:
            if self.vector_error:
                self.aborted = True
                raise self.vector_error
            self.rows = self.vector
        else:
            self.rows = []
        return self

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def rollback(self):
        self.rollbacks += 1
        self.aborted = False


@pytest.fixture
def setup_db(monkeypatch):
    from remote_db import ai_search as search

    monkeypatch.setattr(search._core, "remote_schema", lambda: "test_search")
    monkeypatch.setattr(search._core, "_runtime_env", lambda: {})
    search._vector_index_cache.clear()

    def setup(**kwargs):
        conn = FakeConnection(**kwargs)

        @contextmanager
        def connect():
            conn.checkouts += 1
            yield conn
            assert not conn.aborted, "failed vector transaction leaked to pool"

        monkeypatch.setattr(search._core, "connect", connect)
        return search, conn

    return setup


def test_merge_preserves_lexical_deduplicates_and_applies_vector_threshold(setup_db):
    search, conn = setup_db(lexical=[item("low", .1), item("both", .7), item("null")],
                           vector=[item("both", .7), item("new", .8), item("drop", .44), item("edge", .45)])
    result = search.retrieve("gpt 6", embed=lambda q: [1., 0.])
    assert [r["id"] for r in result["items"]] == ["new", "both", "edge", "low", "null"]
    assert result["items"][1]["legs"] == ["lexical", "vector"]
    assert result["items"][0]["legs"] == ["vector"]
    assert result["lexical_hits"] == 3 and result["vector_hits"] == 4 and result["vector_ok"]
    assert result["query_lexical"] == '("gpt6" OR "gpt 6" OR "gpt-6")'
    assert conn.checkouts == 1
    assert isinstance(result["items"][0]["published_at"], str)


@pytest.mark.parametrize(("threshold", "expected"), [(None, ["low", "null", "high"]), (6, ["null", "high"])])
def test_quality_filter_keeps_unscored(setup_db, threshold, expected):
    search, _ = setup_db(lexical=[item("low", .9, 2), item("null", .8), item("high", .7, 8)])
    result = search.retrieve("query", quality_min=threshold, embed=lambda q: [1., 0.])
    assert [r["id"] for r in result["items"]] == expected


def test_max_items_applied_after_merge_and_sort(setup_db):
    search, _ = setup_db(lexical=[item("low", .1), item("null")], vector=[item("top", .9)])
    assert [r["id"] for r in search.retrieve("q", max_items=1, embed=lambda q: [1., 0.])["items"]] == ["top"]


def test_embedding_failure_keeps_lexical_and_warns(setup_db, capsys):
    search, conn = setup_db(lexical=[item("lex")])

    def fail(q):
        raise RuntimeError("embedding unavailable")

    result = search.retrieve("q", embed=fail)
    assert not result["vector_ok"] and result["vector_hits"] == 0
    assert [r["id"] for r in result["items"]] == ["lex"]
    assert not any("LIMIT 300" in sql for sql, _ in conn.calls)
    assert "NULL::double precision AS sim" in next(sql for sql, _ in conn.calls if "&@~" in sql)
    assert "[warn] ai_search vector leg failed:" in capsys.readouterr().out


def test_vector_timeout_rolls_back_and_keeps_lexical(setup_db, capsys):
    search, conn = setup_db(lexical=[item("lex", .2)], vector_error=TimeoutError("query canceled"))
    result = search.retrieve("q", embed=lambda q: [1., 0.])
    assert not result["vector_ok"] and result["vector_hits"] == 0
    assert [r["id"] for r in result["items"]] == ["lex"]
    assert conn.rollbacks == 1
    assert "vector leg failed:" in capsys.readouterr().out


def test_degraded_results_keep_newest_first_even_with_different_similarities(setup_db):
    newer, older = item("newer", .1), item("older", .9)
    older["published_at"] = datetime(2026, 9, 18, tzinfo=timezone.utc)
    search, _ = setup_db(lexical=[newer, older], vector_error=TimeoutError("query canceled"))
    result = search.retrieve("q", max_items=1, embed=lambda q: [1., 0.])
    assert not result["vector_ok"]
    assert [row["id"] for row in result["items"]] == ["newer"]


def test_lexical_failure_is_not_silently_reported_as_empty_success(setup_db):
    search, _ = setup_db(lexical_error=TimeoutError("lexical timeout"))
    with pytest.raises(TimeoutError, match="lexical timeout"):
        search.retrieve("q", embed=lambda q: [1., 0.])


@pytest.mark.parametrize("index_error", [None, TimeoutError("catalog timeout")])
def test_missing_or_uncheckable_index_skips_vector_scan(setup_db, index_error):
    search, conn = setup_db(index=False, index_error=index_error, lexical=[item("lex")])
    result = search.retrieve("q", embed=lambda q: [1., 0.])
    assert not result["vector_ok"]
    assert not any("LIMIT 300" in sql for sql, _ in conn.calls)
    assert [r["id"] for r in result["items"]] == ["lex"]


def test_index_check_validity_schema_and_ten_minute_cache(setup_db, monkeypatch):
    search, conn = setup_db(index=True)
    now = [100.]
    monkeypatch.setattr(search.time, "monotonic", lambda: now[0])
    assert search.vector_index_available(conn)
    sql, params = next((s, p) for s, p in conn.calls if "pg_indexes" in s)
    assert "indisvalid" in sql and "pg_index" in sql
    assert "test_search" in params and search.VECTOR_INDEX_NAME in params
    conn.index = False
    now[0] += 599
    assert search.vector_index_available(conn)
    now[0] += 2
    assert not search.vector_index_available(conn)
    assert len([s for s, _ in conn.calls if "pg_indexes" in s]) == 2


def test_sql_reuses_index_expression_visibility_and_only_local_settings(setup_db, monkeypatch):
    search, conn = setup_db()
    monkeypatch.setattr(search._core, "_runtime_env", lambda: {
        "INFO2ACTION_AI_SEARCH_LEXICAL_TIMEOUT_MS": "6123", "AI_SEARCH_VECTOR_TIMEOUT_MS": "8456",
    })
    search.retrieve("q", embed=lambda q: [1., 0.], window_days=42)
    lexical = next((s, p) for s, p in conn.calls if "&@~" in s)
    vector = next((s, p) for s, p in conn.calls if "LIMIT 300" in s)
    assert INDEX_EXPRESSION + " &@~ %(lex)s" in lexical[0]
    for sql, params in (lexical, vector):
        assert "test_search.items" in sql and "remote_poc" not in sql
        assert "i.visible = 1" in sql and "make_interval(days => %(window_days)s)" in sql
        assert "i.user_id IS NULL" in sql and "i.platform != 'manual'" in sql
        assert "search:%%" in sql and "stars" in sql
        assert params["window_days"] == 42 and params["qvec"] == "[1,0]"
        assert "score10" in sql and "AS hs10" in sql and "i.media_json" in sql
    assert "i.embedding::extensions.halfvec(1536)" in vector[0]
    assert "ORDER BY i.embedding::extensions.halfvec(1536)" in vector[0]
    assert "i.embedding IS NOT NULL" in vector[0]
    settings = [s for s, _ in conn.calls if s.upper().startswith("SET")]
    assert all(s.upper().startswith("SET LOCAL ") for s in settings)
    assert any("6123ms" in s for s in settings) and any("8456ms" in s for s in settings)
    assert "SET LOCAL hnsw.ef_search = 300" in settings
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in settings


def test_user_status_and_manual_owner_are_parameterized(setup_db):
    search, conn = setup_db()
    search.retrieve("q", public_only=False, user_id="user", manual_owner_user_id="owner", embed=lambda q: [1., 0.])
    for sql, params in conn.calls:
        if "&@~" in sql or "LIMIT 300" in sql:
            assert params["status_user_id"] == "user" and params["manual_owner_user_id"] == "owner"
            assert "LEFT JOIN test_search.item_status" in sql


@pytest.mark.parametrize(("env_cutoff", "injected_cutoff", "expected"), [
    (None, None, "2026-06-20 00:00:00+00"),
    ("2026-07-01", None, "2026-07-01 00:00:00+00"),
    ("2026-07-01", "2026-08-01", "2026-08-01 00:00:00+00"),
    (None, "2026-08-01 12:30:00+08", "2026-08-01 12:30:00+08"),
    (None, "2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
])
def test_vector_index_cutoff_applies_only_to_vector_leg(
    setup_db, monkeypatch, env_cutoff, injected_cutoff, expected,
):
    search, conn = setup_db()
    env = {} if env_cutoff is None else {"AI_SEARCH_VECTOR_INDEX_CUTOFF": env_cutoff}
    monkeypatch.setattr(search._core, "_runtime_env", lambda: env)
    kwargs = {} if injected_cutoff is None else {"vector_index_cutoff": injected_cutoff}
    search.retrieve("q", embed=lambda q: [1., 0.], **kwargs)
    lexical_sql = next(s for s, _ in conn.calls if "&@~" in s)
    vector_sql, vector_params = next((s, p) for s, p in conn.calls if "LIMIT 300" in s)
    assert "AND i.published_at >= %(cutoff)s::timestamptz" in vector_sql
    assert "%(cutoff)s" not in lexical_sql
    assert vector_params["cutoff"] == expected


def test_default_embedding_uses_runtime_provider_query_mode(setup_db, monkeypatch):
    from clustering import embedding_provider

    search, _ = setup_db()
    calls = []
    fake = embedding_provider.FakeEmbeddingProvider()
    original = fake.embed

    def embed(texts, *, mode):
        calls.append((texts, mode))
        return original(texts, mode=mode)

    fake.embed = embed
    monkeypatch.setattr(embedding_provider, "resolve_runtime_provider", lambda: ("fake", "", None))
    monkeypatch.setattr(embedding_provider, "get_provider", lambda name, **kw: fake)
    assert search.retrieve("original query")["vector_ok"]
    assert calls == [(["original query"], "query")]


@pytest.mark.parametrize('env_limit,injected,expected', [(None, None, 2000), ('123', None, 123), ('123', 7, 7)])
def test_lexical_limit_does_not_change_vector_limit(setup_db, monkeypatch, env_limit, injected, expected):
    search, conn = setup_db(lexical=[item('a'), item('b')])
    env = {} if env_limit is None else {'AI_SEARCH_LEXICAL_MAX_ROWS': env_limit}
    monkeypatch.setattr(search._core, '_runtime_env', lambda: env)
    kwargs = {} if injected is None else {'lexical_max_rows': injected}
    result = search.retrieve('AI', embed=lambda q: [1., 0.], max_items=1, **kwargs)
    sql, params = next((s, p) for s, p in conn.calls if '&@~' in s)
    assert 'ORDER BY i.published_at DESC NULLS LAST, i.id LIMIT %(lexical_limit)s' in ' '.join(sql.split())
    assert params['lexical_limit'] == expected
    vector = next(s for s, _ in conn.calls if 'LIMIT 300' in s)
    assert 'lexical_limit' not in vector
    assert result['lexical_hits'] == 2 and len(result['items']) == 1
