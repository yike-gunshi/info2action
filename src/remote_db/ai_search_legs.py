"""Read-only database legs for the AI search v2 retrieval pipeline.

Each call owns one checkout and transaction. Settings stay local to that
transaction, and errors propagate so the pipeline can discard only that leg.
"""
from __future__ import annotations

import math
import re
from typing import Any

from ai_search_digest import _clean, _platform

from . import _core, _base_item_where, _item_status_join, _normalize_item, vector_to_pg
from .ai_search import _LEXICAL_EXPRESSION, _quote_phrase, VECTOR_INDEX_NAME


_MODEL = re.compile(r"^([A-Za-z][A-Za-z.]*)[- ]?(\d+(?:\.\d+)?)$")
_LEXICAL_TIMEOUT_KEY = 'INFO2ACTION_AI_SEARCH_LEXICAL_TIMEOUT_MS'
_VECTOR_TIMEOUT_KEY = 'AI_SEARCH_VECTOR_TIMEOUT_MS'


def pg_query(keyword: str) -> str:
    """Match an entire phrase, including the evaluated model spelling variants."""
    keyword = ' '.join(keyword.split())
    model = _MODEL.fullmatch(keyword)
    if model:
        name, version = model.groups()
        variants = [name + separator + version for separator in ('', ' ', '-')]
    elif ' ' in keyword:
        variants = [keyword, keyword.replace(' ', '')]
    else:
        variants = [keyword]
    return ' OR '.join(_quote_phrase(variant) for variant in dict.fromkeys(variants))


def _timeout_ms(*, vector: bool = False, timeout_s: float | None = None) -> int:
    key, default = (_VECTOR_TIMEOUT_KEY, 8000) if vector else (_LEXICAL_TIMEOUT_KEY, 6000)
    milliseconds = _core._env_int(_core._runtime_env(), key, default, min_value=1)
    if timeout_s is not None:
        seconds = float(timeout_s)
        if not math.isfinite(seconds) or seconds <= 0:
            raise TimeoutError('AI search database leg has no time remaining')
        milliseconds = min(milliseconds, max(1, int(seconds * 1000)))
    return milliseconds


def _set_timeout(conn: Any, timeout_ms: int) -> None:
    # Do not swallow failures here: executing the SELECT without its timeout
    # would violate the caller's deadline.
    conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'")


def _scope(
    *, window_days: int, public_only: bool, user_id: str | None,
    manual_owner_user_id: str | None, min_github_stars: int,
) -> tuple[str, str, list[str], dict]:
    schema = _core.remote_schema()
    where, params = _base_item_where(
        public_only=public_only, manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.extend([
        'i.visible = 1',
        'i.published_at >= now() - make_interval(days => %(window_days)s)',
    ])
    status_join, status_params, _ = _item_status_join(schema, user_id)
    params.update(status_params)
    params['window_days'] = int(window_days)
    return schema, status_join, where, params


def count_phrase(
    keyword: str, *, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None, cap: int | None = None,
) -> int:
    timeout_ms = _timeout_ms(timeout_s=timeout_s)
    schema, status_join, where, params = _scope(
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
    )
    params['lex'] = pg_query(keyword)
    limit_sql = ''
    if cap is not None:
        params['cap'] = max(1, int(cap))
        limit_sql = 'LIMIT %(cap)s'
    with _core.connect() as conn:
        with conn.transaction():
            _set_timeout(conn, timeout_ms)
            row = conn.execute(
                f"""SELECT count(*) AS n FROM (
                    SELECT 1 FROM {schema}.items i {status_join}
                    WHERE {' AND '.join(where)} AND {_LEXICAL_EXPRESSION} &@~ %(lex)s
                    {limit_sql}) t""",
                params,
            ).fetchone()
    return int(row['n']) if row else 0


def lexical_top(
    keyword: str, k: int = 200, *, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None,
) -> list[dict]:
    timeout_ms = _timeout_ms(timeout_s=timeout_s)
    schema, status_join, where, params = _scope(
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
    )
    params.update(lex=pg_query(keyword), k=max(1, int(k)))
    with _core.connect() as conn:
        with conn.transaction():
            _set_timeout(conn, timeout_ms)
            rows = conn.execute(
                f"""SELECT i.id, extensions.pgroonga_score(i.tableoid, i.ctid) AS score
                    FROM {schema}.items i {status_join}
                    WHERE {' AND '.join(where)} AND {_LEXICAL_EXPRESSION} &@~ %(lex)s
                    ORDER BY score DESC, i.published_at DESC NULLS LAST, i.id LIMIT %(k)s""",
                params,
            ).fetchall()
    return [{'id': row['id'], 'score': float(row['score'] or 0)} for row in rows]


def _vector_literal(vector: Any) -> str:
    literal = vector_to_pg(vector)
    if literal in (None, '[]'):
        raise ValueError('empty query embedding')
    return literal


def _require_vector_index(conn: Any, schema: str, timeout_ms: int) -> None:
    # Same validity/ready check as v1, but bounded by this leg's timeout. In
    # particular, a missing index must never turn ANN into a full table scan.
    _set_timeout(conn, timeout_ms)
    row = conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM pg_indexes p
               JOIN pg_namespace n ON n.nspname = p.schemaname
               JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = p.indexname
               JOIN pg_index ix ON ix.indexrelid = c.oid
               WHERE p.schemaname = %s AND p.tablename = 'items'
                 AND p.indexname = %s AND ix.indisvalid AND ix.indisready
           ) AS available""",
        (schema, VECTOR_INDEX_NAME),
    ).fetchone()
    if not row or not row['available']:
        raise RuntimeError('AI search vector index missing or invalid')


def vector_top(
    vector: Any, k: int = 200, *, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None,
    vector_index_cutoff: str | None = None,
) -> list[dict]:
    timeout_ms = _timeout_ms(vector=True, timeout_s=timeout_s)
    schema, status_join, where, params = _scope(
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
    )
    cutoff = vector_index_cutoff if vector_index_cutoff is not None else \
        _core._runtime_env().get('AI_SEARCH_VECTOR_INDEX_CUTOFF', '2026-06-20')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', cutoff):
        cutoff += ' 00:00:00+00'
    params.update(qvec=_vector_literal(vector), k=max(1, int(k)), cutoff=cutoff)
    with _core.connect() as conn:
        with conn.transaction():
            _require_vector_index(conn, schema, timeout_ms)
            conn.execute(f"SET LOCAL hnsw.ef_search = {max(300, params['k'])}")
            conn.execute('SET LOCAL hnsw.iterative_scan = relaxed_order')
            _set_timeout(conn, timeout_ms)
            rows = conn.execute(
                f"""SELECT i.id,
                        1 - (i.embedding OPERATOR(extensions.<=>) %(qvec)s::extensions.vector) AS sim
                    FROM {schema}.items i {status_join}
                    WHERE {' AND '.join(where)} AND i.embedding IS NOT NULL
                      AND i.published_at >= %(cutoff)s::timestamptz
                    ORDER BY i.embedding::extensions.halfvec(1536)
                        OPERATOR(extensions.<=>) %(qvec)s::extensions.halfvec(1536)
                    LIMIT %(k)s""",
                params,
            ).fetchall()
    return [{'id': row['id'], 'sim': float(row['sim'])} for row in rows if row['sim'] is not None]


def _id_rows(
    ids: list, columns: str, *, window_days: int, public_only: bool,
    user_id: str | None, manual_owner_user_id: str | None, min_github_stars: int,
    timeout_s: float | None, vector: Any = None, column_params: dict | None = None,
) -> list[dict]:
    if not ids:
        return []
    timeout_ms = _timeout_ms(vector=vector is not None, timeout_s=timeout_s)
    schema, status_join, where, params = _scope(
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
    )
    where.append('i.id = ANY(%(ids)s)')
    params['ids'] = list(ids)
    params.update(column_params or {})
    if vector is not None:
        where.append('i.embedding IS NOT NULL')
        params['qvec'] = _vector_literal(vector)
    with _core.connect() as conn:
        with conn.transaction():
            _set_timeout(conn, timeout_ms)
            rows = conn.execute(
                f"SELECT {columns} FROM {schema}.items i {status_join} WHERE {' AND '.join(where)}",
                params,
            ).fetchall()
    return rows


def fetch_meta(
    ids: list, *, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None, summary_chars: int = 1000,
) -> dict:
    rows = _id_rows(
        ids, 'i.id, i.title, left(i.ai_summary, %(summary_chars)s) AS ai_summary, '
             'i.platform, i.published_at, i.fetched_at, i.author_name, i.url',
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
        timeout_s=timeout_s, column_params={'summary_chars': max(0, int(summary_chars))},
    )
    return {row['id']: _normalize_item(dict(row)) for row in rows}


def similarities(
    ids: list, vector: Any, *, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None,
) -> dict:
    if not ids:
        return {}
    if vector is None:
        raise ValueError('empty query embedding')
    rows = _id_rows(
        ids, 'i.id, 1 - (i.embedding OPERATOR(extensions.<=>) %(qvec)s::extensions.vector) AS sim',
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
        timeout_s=timeout_s, vector=vector,
    )
    return {row['id']: float(row['sim']) for row in rows if row['sim'] is not None}


def _input_length(item: dict, number: int) -> int:
    published = item.get('published_at') or item.get('fetched_at')
    date = str(published)[:10] if published else '日期未知'
    header = f'[{number}] {_platform(item)} · {date} · {_clean(item.get("title"))[:80]}\n正文：'
    return len(header) + int(item['body_chars'])


def input_lengths(
    ids: list, *, item_chars: int = 4000, window_days: int = 90, public_only: bool = True,
    user_id: str | None = None, manual_owner_user_id: str | None = None,
    min_github_stars: int = 50, timeout_s: float | None = None,
) -> dict:
    """Count each digest header and its first available body, without budget downgrades.

    SQL returns numeric body lengths, capped content first, then key points and
    summary. The user-message prefix and separators are counted by the pipeline.
    Numbering follows the requested candidates still visible, not SQL row order.
    """
    # Trim again after replacing whitespace so leading/trailing tabs and newlines
    # match digest._clean, including bodies consisting entirely of whitespace.
    content_chars = r"char_length(btrim(regexp_replace(btrim(i.content), '\s+', ' ', 'g')))"
    summary_chars = r"char_length(btrim(regexp_replace(btrim(i.ai_summary), '\s+', ' ', 'g')))"
    points_clean = r"btrim(regexp_replace(btrim(i.ai_key_points::text), '\s+', ' ', 'g'))"
    rows = _id_rows(
        ids, f'''i.id, left(i.title, 300) AS title, i.platform, i.published_at, i.fetched_at,
            CASE WHEN {content_chars} > %(item_chars)s
                     THEN %(item_chars)s + char_length(%(truncated_suffix)s)
                 WHEN {content_chars} > 0 THEN {content_chars}
                 WHEN {points_clean} NOT IN ('', '[]', '{{}}', 'null', 'false', '0')
                     THEN char_length(i.ai_key_points::text)
                 ELSE COALESCE({summary_chars}, 0)
            END AS body_chars''',
        window_days=window_days, public_only=public_only, user_id=user_id,
        manual_owner_user_id=manual_owner_user_id, min_github_stars=min_github_stars,
        timeout_s=timeout_s, column_params={
            'item_chars': max(0, int(item_chars)), 'truncated_suffix': '…（已截）',
        },
    )
    by_id = {row['id']: _normalize_item(dict(row), detail=True) for row in rows}
    ordered = [item_id for item_id in ids if item_id in by_id]
    return {
        item_id: _input_length(by_id[item_id], number)
        for number, item_id in enumerate(ordered, 1)
    }
