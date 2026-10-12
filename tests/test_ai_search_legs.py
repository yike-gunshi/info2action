import json
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from remote_db import ai_search_legs as legs
import ai_search_digest as digest


class FakeConnection:
    def __init__(self, rows=(), *, index=True, error=None):
        self.result_rows = list(rows)
        self.index, self.error = index, error
        self.calls = []
        self.rows = []
        self.in_transaction = False
        self.transactions = 0
        self.checkouts = 0

    @contextmanager
    def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        self.transactions += 1
        try:
            yield self
        finally:
            self.in_transaction = False

    def execute(self, sql, params=None):
        assert self.in_transaction, 'queries and settings must be transaction-local'
        self.calls.append((sql, params))
        if sql.lstrip().upper().startswith('SELECT'):
            assert len(self.calls) >= 2
            assert self.calls[-2][0].startswith('SET LOCAL statement_timeout = ')
            if self.error:
                raise self.error
            self.rows = [{'available': self.index}] if 'pg_indexes' in sql else self.result_rows
        else:
            assert sql.startswith('SET LOCAL '), 'session settings leak through transaction pooling'
            self.rows = []
        return self

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


@pytest.fixture
def setup_db(monkeypatch):
    monkeypatch.setattr(legs._core, 'remote_schema', lambda: 'test_search')
    monkeypatch.setattr(legs._core, '_runtime_env', lambda: {})

    def setup(rows=(), **kw):
        conn = FakeConnection(rows, **kw)

        @contextmanager
        def connect():
            conn.checkouts += 1
            yield conn

        monkeypatch.setattr(legs._core, 'connect', connect)
        return conn

    return setup


@pytest.mark.parametrize('keyword,expected', [
    ('Sora 3', '"Sora3" OR "Sora 3" OR "Sora-3"'),
    ('gpt-3.5', '"gpt3.5" OR "gpt 3.5" OR "gpt-3.5"'),
    ('GPT3.5', '"GPT3.5" OR "GPT 3.5" OR "GPT-3.5"'),
    ('GPT. 3', '"GPT.3" OR "GPT. 3" OR "GPT.-3"'),
    ('  personal \n Agent  ', '"personal Agent" OR "personalAgent"'),
    ('Muse', '"Muse"'),
    ('a"b\\c', '"a\\"b\\\\c"'),
])
def test_phrase_query_keeps_whole_phrase_and_expands_models(keyword, expected):
    assert legs.pg_query(keyword) == expected


CALLS = [
    lambda **kw: legs.count_phrase('Sora 3', **kw),
    lambda **kw: legs.lexical_top('Sora 3', k=17, **kw),
    lambda **kw: legs.vector_top([1., 0.], k=17, **kw),
    lambda **kw: legs.fetch_meta(['id'], **kw),
    lambda **kw: legs.similarities(['id'], [1., 0.], **kw),
    lambda **kw: legs.input_lengths(['id'], **kw),
]


@pytest.mark.parametrize('call', CALLS)
def test_all_item_queries_reuse_public_visibility_and_window(setup_db, call):
    conn = setup_db([{'id': 'id', 'n': 7, 'score': 1.5, 'sim': .7, 'body_chars': 0}])
    call(window_days=7)
    selects = [(sql, params) for sql, params in conn.calls if sql.lstrip().startswith('SELECT') and 'pg_indexes' not in sql]
    assert len(selects) == 1
    sql, params = selects[0]
    assert 'FROM test_search.items i' in sql
    assert "i.platform != 'manual'" in sql and 'i.user_id IS NULL' in sql
    assert 'i.visible = 1' in sql and "source NOT LIKE 'search:%%'" in sql
    assert 'stars' in sql and '>= 50' in sql
    assert 'i.published_at >= now() - make_interval(days => %(window_days)s)' in sql
    assert params['window_days'] == 7
    assert conn.checkouts == 1 and conn.transactions == 1
    assert all(sql.startswith('SET LOCAL ') for sql, _ in conn.calls if sql.startswith('SET'))


@pytest.mark.parametrize('call', CALLS)
def test_all_item_queries_reuse_viewer_and_manual_owner_scope(setup_db, call):
    conn = setup_db([{'id': 'id', 'n': 7, 'score': 1.5, 'sim': .7, 'body_chars': 0}])
    call(public_only=False, user_id='viewer', manual_owner_user_id='owner', min_github_stars=123)
    sql, params = next((sql, params) for sql, params in conn.calls if 'FROM test_search.items i' in sql)
    assert 'LEFT JOIN test_search.item_status s ON s.item_id = i.id AND s.user_id = %(status_user_id)s' in sql
    assert 'i.user_id = %(manual_owner_user_id)s' in sql
    assert params['status_user_id'] == 'viewer' and params['manual_owner_user_id'] == 'owner'
    assert '>= 123' in sql


def test_lexical_count_and_rank_use_index_expression_and_config_timeout(setup_db, monkeypatch):
    from remote_db.ai_search import _LEXICAL_EXPRESSION

    monkeypatch.setattr(legs._core, '_runtime_env', lambda: {'INFO2ACTION_AI_SEARCH_LEXICAL_TIMEOUT_MS': '6123'})
    conn = setup_db([{'n': 23}])
    assert legs.count_phrase('Sora 3') == 23
    sql, params = conn.calls[-1]
    assert 'SELECT count(*) AS n' in sql
    assert _LEXICAL_EXPRESSION + ' &@~ %(lex)s' in sql
    assert params['lex'] == legs.pg_query('Sora 3')
    assert conn.calls[-2][0] == "SET LOCAL statement_timeout = '6123ms'"
    conn = setup_db([{'id': 'a', 'score': 12}, {'id': 'b', 'score': None}])
    assert legs.lexical_top('Muse', k=17) == [{'id': 'a', 'score': 12.}, {'id': 'b', 'score': 0.}]
    sql, params = conn.calls[-1]
    assert 'extensions.pgroonga_score(i.tableoid, i.ctid) AS score' in sql
    assert 'ORDER BY score DESC, i.published_at DESC NULLS LAST, i.id' in sql
    assert 'LIMIT %(k)s' in sql and params['k'] == 17


def test_count_phrase_stops_at_parameterized_cap(setup_db):
    conn = setup_db([{'n': 1501}])
    assert legs.count_phrase('使用', cap=1501) == 1501
    sql, params = conn.calls[-1]
    assert 'SELECT count(*) AS n FROM (' in sql and 'SELECT 1 FROM test_search.items' in sql
    assert 'LIMIT %(cap)s' in sql and params['cap'] == 1501
    assert '1501' not in sql and params['lex'] == legs.pg_query('使用')


@pytest.mark.parametrize('cutoff,expected', [
    (None, '2026-07-01 00:00:00+00'),
    ('2026-08-01', '2026-08-01 00:00:00+00'),
    ('2026-08-01T04:00:00Z', '2026-08-01T04:00:00Z'),
])
def test_vector_matches_partial_index_cutoff_and_settings(setup_db, monkeypatch, cutoff, expected):
    from remote_db.ai_search import VECTOR_INDEX_NAME

    monkeypatch.setattr(legs._core, '_runtime_env', lambda: {
        'AI_SEARCH_VECTOR_TIMEOUT_MS': '8456', 'AI_SEARCH_VECTOR_INDEX_CUTOFF': '2026-07-01',
    })
    conn = setup_db([{'id': 'a', 'sim': .8}])
    assert legs.vector_top([1., 0.], 321, vector_index_cutoff=cutoff) == [{'id': 'a', 'sim': .8}]
    index_sql, index_params = next((sql, params) for sql, params in conn.calls if 'pg_indexes' in sql)
    assert 'ix.indisvalid' in index_sql and 'ix.indisready' in index_sql
    assert 'test_search' in index_params and VECTOR_INDEX_NAME in index_params
    sql, params = conn.calls[-1]
    assert 'ORDER BY i.embedding::extensions.halfvec(1536)' in sql
    assert 'OPERATOR(extensions.<=>) %(qvec)s::extensions.halfvec(1536)' in sql
    assert 'i.embedding IS NOT NULL' in sql
    assert 'i.published_at >= %(cutoff)s::timestamptz' in sql
    assert params['cutoff'] == expected and params['qvec'] == '[1,0]' and params['k'] == 321
    settings = [sql for sql, _ in conn.calls if sql.startswith('SET')]
    assert "SET LOCAL statement_timeout = '8456ms'" in settings
    assert 'SET LOCAL hnsw.ef_search = 321' in settings
    assert 'SET LOCAL hnsw.iterative_scan = relaxed_order' in settings


def test_missing_vector_index_raises_before_scanning_items(setup_db):
    conn = setup_db(index=False)
    with pytest.raises(RuntimeError, match='index'):
        legs.vector_top([1., 0.])
    assert not any('FROM test_search.items' in sql for sql, _ in conn.calls)


@pytest.mark.parametrize('call', [
    lambda vector: legs.vector_top(vector),
    lambda vector: legs.similarities(['a'], vector),
])
@pytest.mark.parametrize('vector', [None, []])
def test_empty_vectors_fail_before_database_checkout(setup_db, call, vector):
    conn = setup_db()
    with pytest.raises(ValueError, match='empty query embedding'):
        call(vector)
    assert conn.checkouts == 0


@pytest.mark.parametrize('call', CALLS)
def test_remaining_budget_caps_each_statement_timeout(setup_db, call):
    conn = setup_db([{'id': 'id', 'n': 7, 'score': 1.5, 'sim': .7, 'body_chars': 0}])
    call(timeout_s=.123)
    assert all(sql == "SET LOCAL statement_timeout = '123ms'" for sql, _ in conn.calls if 'statement_timeout' in sql)


def test_zero_timeout_rejects_query_without_checking_out_connection(setup_db):
    conn = setup_db()
    with pytest.raises(TimeoutError):
        legs.count_phrase('Muse', timeout_s=0)
    assert conn.checkouts == 0


def test_timeout_and_query_errors_propagate(setup_db):
    setup_db(error=TimeoutError('query canceled'))
    with pytest.raises(TimeoutError, match='query canceled'):
        legs.lexical_top('Muse')


def test_meta_normalizes_dates_and_similarity_is_not_index_cutoff_filtered(setup_db):
    published = datetime(2026, 10, 1, tzinfo=timezone.utc)
    conn = setup_db([{'id': 'a', 'title': '标题', 'ai_summary': '摘要', 'platform': 'rss', 'published_at': published}])
    meta = legs.fetch_meta(['a', 'invisible'])
    assert set(meta) == {'a'} and isinstance(meta['a']['published_at'], str)
    assert conn.calls[-1][1]['ids'] == ['a', 'invisible']
    conn = setup_db([{'id': 'a', 'sim': .75}, {'id': 'no-vector', 'sim': None}])
    assert legs.similarities(['a', 'no-vector'], [1., 0.]) == {'a': .75}
    sql, params = conn.calls[-1]
    assert 'i.id = ANY(%(ids)s)' in sql and 'i.embedding IS NOT NULL' in sql
    assert '::extensions.vector' in sql and '%(cutoff)s' not in sql
    assert params['qvec'] == '[1,0]'


@pytest.mark.parametrize('summary_chars', [None, 0, 37])
def test_meta_only_returns_a_parameterized_summary_prefix(setup_db, summary_chars):
    conn = setup_db([{'id': 'a', 'ai_summary': '短摘要'}])
    kwargs = {} if summary_chars is None else {'summary_chars': summary_chars}
    assert legs.fetch_meta(['a'], **kwargs)['a']['ai_summary'] == '短摘要'
    sql, params = conn.calls[-1]
    assert 'left(i.ai_summary, %(summary_chars)s) AS ai_summary' in sql
    assert 'i.content' not in sql
    assert params['summary_chars'] == (1000 if summary_chars is None else summary_chars)


@pytest.mark.parametrize('call', [
    lambda: legs.fetch_meta([]),
    lambda: legs.similarities([], [1., 0.]),
    lambda: legs.input_lengths([]),
])
def test_empty_id_lists_do_not_connect(setup_db, call):
    conn = setup_db()
    assert call() == {} and conn.checkouts == 0


def _input_length(item, number, item_chars):
    """The original full-text estimator, kept here as the regression oracle."""
    published = item.get('published_at') or item.get('fetched_at')
    date = str(published)[:10] if published else '日期未知'
    header = f'[{number}] {digest._platform(item)} · {date} · {digest._clean(item.get("title"))[:80]}\n正文：'
    content = digest._clean(item.get('content'))
    if len(content) > item_chars:
        content = content[:item_chars] + '…（已截）'
    points = item.get('ai_key_points')
    if isinstance(points, str):
        points = points if points.strip() else ''
    else:
        points = json.dumps(points, ensure_ascii=False) if points else ''
    body = content or points or digest._clean(item.get('ai_summary'))
    return len(header) + len(body)


def _length_row(item, item_chars):
    """Fake the numeric body projection; no full body travels to Python."""
    content_chars = len(digest._clean(item.get('content')))
    points = item.get('ai_key_points')
    if isinstance(points, str):
        points_chars = len(points) if points.strip() else 0
    else:
        points_chars = len(json.dumps(points, ensure_ascii=False)) if points else 0
    if content_chars > item_chars:
        body_chars = item_chars + len('…（已截）')
    else:
        body_chars = content_chars or points_chars or len(digest._clean(item.get('ai_summary')))
    return {**{key: item[key] for key in ('id', 'platform', 'published_at', 'fetched_at') if key in item},
            'title': (item.get('title') or '')[:300], 'body_chars': body_chars}


@pytest.mark.parametrize('item_chars', [0, 5, 4000])
def test_input_lengths_match_digest_headers_and_all_three_body_tiers(setup_db, item_chars):
    candidates = [
        {'id': 'content', 'title': ' 标题\t\n' + '长' * 90, 'platform': 'twitter', 'content': '\n\t 12345\n6 \r\n',
         'published_at': '2026-10-01T12:00:00Z', 'ai_summary': '备用摘要'},
        {'id': 'long', 'platform': 'rss', 'content': '长' * 5000, 'ai_key_points': ['备用']},
        {'id': 'points-str', 'platform': 'github', 'title': '标题\n二', 'content': ' \n ',
         'ai_key_points': '原样\n要点', 'fetched_at': '2026-09-30'},
        {'id': 'points-json', 'platform': 'rss', 'ai_key_points': [{'title': '要点', 'points': ['一']}]},
        {'id': 'summary', 'ai_key_points': [], 'ai_summary': '\t 摘要\n内容 \r\n'},
        {'id': 'empty', 'ai_key_points': '  '},
    ]
    # Returning rows in a different order proves numbering follows requested ids.
    conn = setup_db(_length_row(item, item_chars) for item in reversed(candidates))
    ids = [item['id'] for item in candidates]
    lengths = legs.input_lengths(ids, item_chars=item_chars)
    assert lengths == {item['id']: _input_length(item, number, item_chars)
                       for number, item in enumerate(candidates, 1)}
    output = digest._build_input('q', 'today', candidates, {
        'AI_SEARCH_ITEM_CHARS': item_chars, 'AI_SEARCH_INPUT_BUDGET_TOKENS': 10**9,
    })['user_message']
    prefix = f'用户问题：q\n\n今天：today\n\n候选条目（共 {len(candidates)} 条）：\n\n'
    assert sum(lengths.values()) + len(prefix) + 2 * (len(candidates) - 1) == len(output)
    # Check every item rather than allowing opposite length errors to cancel.
    blocks = re.split(r'\n\n(?=\[\d+\])', output[len(prefix):])
    assert lengths == {item_id: len(block) for item_id, block in zip(ids, blocks)}
    assert conn.calls[-1][1]['ids'] == ids
    sql, params = conn.calls[-1]
    assert 'left(i.title, 300) AS title' in sql
    assert "regexp_replace(btrim(i.content), '\\s+', ' ', 'g')" in sql
    assert 'char_length(i.ai_key_points::text)' in sql
    assert 'AS body_chars' in sql and '%(item_chars)s' in sql
    assert params['item_chars'] == item_chars
    assert params['truncated_suffix'] == '…（已截）'
    assert not re.search(r'i\.(content|ai_key_points|ai_summary)\s*(?:,|AS\s|FROM\s)', sql, re.I)


def test_input_lengths_number_only_candidates_still_in_scope(setup_db):
    candidate = {'id': 'kept', 'title': '标题', 'content': '正文'}
    setup_db([_length_row(candidate, 4000)])
    result = legs.input_lengths(['removed', 'kept'])
    assert result == {'kept': len('[1]  · 日期未知 · 标题\n正文：正文')}


@pytest.fixture(scope='module')
def lengths_postgres(tmp_path_factory):
    pgserver = pytest.importorskip('pgserver')
    pgdata = tmp_path_factory.mktemp('ai-search-lengths-postgres')
    try:
        server = pgserver.get_server(pgdata, cleanup_mode='delete')
    except (OSError, subprocess.CalledProcessError) as exc:
        log = (pgdata / 'log').read_text() if (pgdata / 'log').exists() else ''
        if 'operation not permitted' in (str(exc) + log).lower() or isinstance(exc, PermissionError):
            pytest.skip('sandbox prevents starting local PostgreSQL')
        raise
    with server:
        yield server.get_uri()


@pytest.mark.parametrize('points_type', ['text', 'jsonb'])
def test_numeric_lengths_and_summary_prefix_execute_in_postgres(lengths_postgres, monkeypatch, points_type):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    schema = 'lengths_' + points_type

    @contextmanager
    def connect():
        with psycopg.connect(lengths_postgres, row_factory=dict_row) as conn:
            yield conn

    monkeypatch.setattr(legs._core, 'connect', connect)
    monkeypatch.setattr(legs._core, 'remote_schema', lambda: schema)
    monkeypatch.setattr(legs._core, '_runtime_env', lambda: {})
    today = datetime.now(timezone.utc).isoformat()
    points = [{'title': '要点', 'points': ['一', '二']}]
    candidates = [
        {'id': 'spaces', 'title': ' 标题\t\n' + '长' * 90, 'content': '\n \t 正文\r\n 内容 \t'},
        {'id': 'long', 'content': '长' * 5000},
        {'id': 'points', 'content': '\n\t ', 'ai_key_points': points, 'ai_summary': '备用'},
        {'id': 'summary', 'ai_key_points': [], 'ai_summary': '\n \t 摘要\r\n 内容 \t'},
        {'id': 'null', 'ai_summary': None},
    ]
    if points_type == 'text':
        candidates.append({'id': 'string', 'ai_key_points': ' 原样\n要点 '})
    for item in candidates:
        item.update(platform='rss', published_at=today, fetched_at=today)
    with connect() as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
        conn.execute(f'''CREATE TABLE {schema}.items (
            id text PRIMARY KEY, platform text, source text, user_id text,
            visible integer DEFAULT 1, metrics_json jsonb,
            title text, content text, ai_key_points {points_type}, ai_summary text,
            published_at timestamptz, fetched_at timestamptz, author_name text, url text)''')
        for item in candidates:
            key_points = item.get('ai_key_points')
            if points_type == 'jsonb':
                key_points = Jsonb(key_points) if key_points is not None else None
            elif key_points is not None and not isinstance(key_points, str):
                key_points = json.dumps(key_points, ensure_ascii=False)
            conn.execute(f'''INSERT INTO {schema}.items
                (id, platform, title, content, ai_key_points, ai_summary, published_at, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                (item['id'], item['platform'], item.get('title'), item.get('content'),
                 key_points, item.get('ai_summary'), today, today))
        column_type = conn.execute('''SELECT data_type FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'items' AND column_name = 'ai_key_points' ''',
            (schema,)).fetchone()['data_type']
        assert column_type == points_type
        stored_chars = conn.execute(f'''SELECT char_length(ai_key_points::text) AS n
            FROM {schema}.items WHERE id = 'points' ''').fetchone()['n']
        assert stored_chars == len(json.dumps(points, ensure_ascii=False))
        conn.execute(f'UPDATE {schema}.items SET ai_summary = %s WHERE id = %s', ('摘' * 1500, 'long'))
    ids = ['invisible'] + [item['id'] for item in reversed(candidates)]
    ordered = list(reversed(candidates))
    for item_chars in (0, 5, 4000):
        assert legs.input_lengths(ids, item_chars=item_chars) == {
            item['id']: _input_length(item, number, item_chars)
            for number, item in enumerate(ordered, 1)
        }
    assert legs.fetch_meta(ids)['long']['ai_summary'] == '摘' * 1000
    assert legs.fetch_meta(ids, summary_chars=17)['long']['ai_summary'] == '摘' * 17
