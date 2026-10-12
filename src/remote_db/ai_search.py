"""AI search retrieval and durable job storage (independent of feed search)."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from datetime import datetime
from uuid import UUID

from . import _core
# Legacy segments are exec-loaded into the package namespace, not standalone
# modules. Reuse their public-in-package helpers without importing the segments.
from . import (
    _base_item_where,
    _feed_cols,
    _item_status_join,
    _normalize_item,
    _set_short_statement_timeout,
    vector_to_pg,
)


_INTENT_WORDS = frozenset(
    "我想 想 搜索 搜 查 看看 了解 学习 最新 最近 怎么 如何 怎样 教程 使用 用法 是什么 一下 吗 呢 的 和 与 跟".split()
)
_INTENT_PATTERN = "|".join(re.escape(w) for w in sorted(_INTENT_WORDS, key=lambda w: (-len(w), w)))
_LEADING_INTENT = re.compile(rf"^(?:{_INTENT_PATTERN})")
_TRAILING_INTENT = re.compile(rf"(?:{_INTENT_PATTERN})$")
_MODEL = re.compile(r"^([A-Za-z]+)[- ]?(\d+(?:\.\d+)?)$")
VECTOR_INDEX_NAME = "remote_poc_items_embedding_halfvec_hnsw_idx"
_vector_index_cache: dict[str, tuple[float, bool]] = {}
_schema_ready_cache: dict[str, tuple[float, bool]] = {}
_INDEX_CACHE_SECONDS = 600
_SCHEMA_NOT_READY_CACHE_SECONDS = 30

# Preserve the PGroonga expression's left-associative concatenation tree.
_LEXICAL_EXPRESSION = (
    "(((((((COALESCE(i.title,'') || ' ') || COALESCE(i.author_name,'')) || ' ') "
    "|| COALESCE(i.ai_summary,'')) || ' ') || COALESCE(i.ai_keywords,'')))"
)


def _quote_phrase(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lexical_query(q: str) -> str:
    """Normalize width, strip boundary intent words and expand model spellings."""
    # Convert only full-width ASCII/space; NFKC also changes unrelated symbols.
    original = "".join(
        " " if c == "\u3000" else chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
        for c in q
    ).strip()
    text = original
    while text:
        stripped = _TRAILING_INTENT.sub("", _LEADING_INTENT.sub("", text).strip()).strip()
        if stripped == text:
            break
        text = stripped
    tokens = [token for token in text.split() if token not in _INTENT_WORDS]
    if not tokens:
        return _quote_phrase(original)
    phrases = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        match = _MODEL.fullmatch(token)
        if not match and index + 1 < len(tokens):
            match = _MODEL.fullmatch(token + " " + tokens[index + 1])
            if match:
                index += 1
        if match:
            name, number = match.groups()
            phrases.append("(" + " OR ".join(_quote_phrase(name + sep + number) for sep in ("", " ", "-")) + ")")
        else:
            phrases.append(_quote_phrase(token))
        index += 1
    return " ".join(phrases)


def _timeout(key: str, default: int) -> int:
    return _core._env_int(_core._runtime_env(), key, default, min_value=1)


def vector_index_available(conn: Any) -> bool:
    """Check the named index is VALID; cache both outcomes for ten minutes."""
    schema = _core.remote_schema()
    now = time.monotonic()
    cached = _vector_index_cache.get(schema)
    if cached is not None and now - cached[0] < _INDEX_CACHE_SECONDS:
        return cached[1]
    _set_short_statement_timeout(conn, 6000)
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
    available = bool(row and row["available"])
    _vector_index_cache[schema] = (now, available)
    if not available:
        print("[warn] ai_search vector leg failed: HNSW index missing or invalid")
    return available


def schema_ready() -> bool:
    """Cache readiness for 600s, absence for 30s; query errors propagate."""
    schema = _core.remote_schema()
    now = time.monotonic()
    cached = _schema_ready_cache.get(schema)
    if cached is not None:
        ttl = _INDEX_CACHE_SECONDS if cached[1] else _SCHEMA_NOT_READY_CACHE_SECONDS
        if now - cached[0] < ttl:
            return cached[1]
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM pg_class t
                   JOIN pg_namespace n ON n.oid = t.relnamespace
                   JOIN pg_attribute a ON a.attrelid = t.oid
                   WHERE n.nspname = %s AND t.relname = 'ai_search_jobs'
                     AND a.attname = 'write_queued_at' AND NOT a.attisdropped
                     AND EXISTS (
                         SELECT 1 FROM pg_constraint c
                         JOIN pg_attribute s ON s.attrelid = c.conrelid
                         WHERE c.conrelid = t.oid AND c.contype = 'c' AND c.convalidated
                           AND s.attname = 'status' AND s.attnum = ANY(c.conkey)
                           AND position('''retrieving''' in pg_get_constraintdef(c.oid)) > 0
                     )
               ) AS available""", (schema,),
        ).fetchone()
    available = bool(row and row["available"])
    _schema_ready_cache[schema] = (now, available)
    return available


def _embed_query(q: str) -> Any:
    from clustering import embedding_provider

    name, api_key, api_base = embedding_provider.resolve_runtime_provider()
    provider = embedding_provider.get_provider(name, api_key=api_key, api_base=api_base)
    return provider.embed([q], mode="query")[0]


def retrieve(
    q: str, *, window_days=90, sim_min=0.45, quality_min=None, max_items=100,
    public_only=True, user_id=None, manual_owner_user_id=None,
    min_github_stars=50, embed: Callable[[str], Any] | None = None,
    vector_index_cutoff=None, lexical_max_rows=None,
) -> dict:
    """Retrieve lexical matches plus ANN candidates using one DB checkout.

    The injectable embed callable accepts the original query and returns one
    vector. The two SQL legs run serially on one connection; lexical rows are
    retained before running ANN so rollback on a vector failure cannot lose them.
    """
    lex = lexical_query(q)
    qvec = None
    try:
        qvec = vector_to_pg((embed or _embed_query)(q))
        if qvec in (None, "[]"):
            raise ValueError("empty query embedding")
    except Exception as exc:
        qvec = None
        print(f"[warn] ai_search vector leg failed: {exc}")

    schema = _core.remote_schema()
    where, params = _base_item_where(
        public_only=public_only, manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.extend([
        "i.visible = 1",
        "i.published_at >= now() - make_interval(days => %(window_days)s)",
    ])
    status_join, status_params, status_alias = _item_status_join(schema, user_id)
    params.update(status_params)
    params.update(window_days=int(window_days), lex=lex, qvec=qvec,
                  lexical_limit=_timeout("AI_SEARCH_LEXICAL_MAX_ROWS", 2000)
                  if lexical_max_rows is None else max(1, int(lexical_max_rows)))
    vector_rows = []
    vector_ok = False
    with _core.connect() as conn:
        index_ok = False
        if qvec is not None:
            try:
                index_ok = vector_index_available(conn)
            except Exception as exc:
                conn.rollback()
                print(f"[warn] ai_search vector leg failed: {exc}")

        similarity = (
            "1 - (i.embedding OPERATOR(extensions.<=>) %(qvec)s::extensions.vector) AS sim"
            if qvec is not None else "NULL::double precision AS sim"
        )
        columns = (
            _feed_cols(status_alias, include_heavy_json=False) + ", " + similarity
            + ", (i.highlight_scores->'v26'->>'score10')::float AS hs10"
        )
        _set_short_statement_timeout(conn, _timeout("INFO2ACTION_AI_SEARCH_LEXICAL_TIMEOUT_MS", 6000))
        lexical_rows = conn.execute(
            f"""SELECT {columns} FROM {schema}.items i {status_join}
                WHERE {' AND '.join(where)} AND {_LEXICAL_EXPRESSION} &@~ %(lex)s
                ORDER BY i.published_at DESC NULLS LAST, i.id LIMIT %(lexical_limit)s""",
            params,
        ).fetchall()
        if index_ok:
            try:
                _set_short_statement_timeout(conn, _timeout("AI_SEARCH_VECTOR_TIMEOUT_MS", 8000))
                conn.execute("SET LOCAL hnsw.ef_search = 300")
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                cutoff = vector_index_cutoff if vector_index_cutoff is not None else \
                    _core._runtime_env().get("AI_SEARCH_VECTOR_INDEX_CUTOFF", "2026-06-20")
                # Match the partial index's timestamptz constant without a
                # session-timezone-dependent date conversion.
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
                    cutoff = f"{cutoff} 00:00:00+00"
                # The ORDER BY must match the halfvec expression index. A raw
                # vector ORDER BY cannot use it. Keep full precision for sim.
                # https://github.com/pgvector/pgvector#half-precision-indexing
                vector_rows = conn.execute(
                    f"""SELECT {columns} FROM {schema}.items i {status_join}
                        WHERE {' AND '.join(where)} AND i.embedding IS NOT NULL
                        AND i.published_at >= %(cutoff)s::timestamptz
                        ORDER BY i.embedding::extensions.halfvec(1536)
                            OPERATOR(extensions.<=>) %(qvec)s::extensions.halfvec(1536)
                        LIMIT 300""",
                    {**params, "cutoff": cutoff},
                ).fetchall()
                vector_ok = True
            except Exception as exc:
                conn.rollback()
                print(f"[warn] ai_search vector leg failed: {exc}")

    merged = {}
    for row in lexical_rows:
        merged[row["id"]] = {**_normalize_item(dict(row)), "legs": ["lexical"]}
    for row in vector_rows:
        key = row["id"]
        if key in merged:
            if "vector" not in merged[key]["legs"]:
                merged[key]["legs"].append("vector")
        elif row.get("sim") is not None and row["sim"] >= sim_min:
            merged[key] = {**_normalize_item(dict(row)), "legs": ["vector"]}
    items = [item for item in merged.values() if not (
        quality_min is not None and item.get("hs10") is not None and item["hs10"] < quality_min
    )]
    if vector_ok:
        items.sort(key=lambda item: (item.get("sim") is not None, item.get("sim") or 0), reverse=True)
    # Degraded lexical-only results retain SQL's newest-first ordering (R3).
    return dict(
        items=items[:max(0, int(max_items))], lexical_hits=len(lexical_rows),
        vector_hits=len(vector_rows), vector_ok=vector_ok, query_lexical=lex,
    )


def _without_nuls(value: Any) -> Any:
    """PostgreSQL text and jsonb cannot store NUL characters."""
    if isinstance(value, str):
        return value.replace('\x00', '')
    if isinstance(value, dict):
        return {_without_nuls(k): _without_nuls(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_nuls(v) for v in value]
    return value


def _json(value: Any) -> str | None:
    return json.dumps(_without_nuls(value), ensure_ascii=False) if value is not None else None


def _bounded_raw(raw: str) -> str:
    return _without_nuls(raw).encode('utf-8')[:200 * 1024].decode('utf-8', errors='ignore')


def get_candidates(
    candidate_ids: list, *, public_only: bool = True, user_id: str | None = None,
    manual_owner_user_id: str | None = None, min_github_stars: int = 50,
    full_text: bool = False,
) -> list:
    """Reload visible candidates in their original order with the viewer's scope."""
    if not candidate_ids:
        return []
    schema = _core.remote_schema()
    where, params = _base_item_where(
        public_only=public_only, manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.extend(["i.visible = 1", "i.id = ANY(%(ids)s)"])
    status_join, status_params, status_alias = _item_status_join(schema, user_id)
    params.update(status_params)
    params["ids"] = list(candidate_ids)
    columns = _feed_cols(status_alias, include_heavy_json=False)
    if full_text:
        # Keep the card projection light, loading only the digest's source text.
        # ai_summary is already selected by _feed_cols.
        columns = columns.replace("NULL::text AS ai_key_points", "i.ai_key_points") + ", i.content"
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        rows = conn.execute(
            f"SELECT {columns} FROM {schema}.items i {status_join} WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
    by_id = {str(row["id"]): _normalize_item(dict(row), detail=full_text) for row in rows}
    return [by_id[str(item_id)] for item_id in candidate_ids if str(item_id) in by_id]


class QuotaExceeded(Exception):
    """A daily account or site quota was reached before inserting a job."""


class RetrievalInProgress(Exception):
    """The account already has a job finding sources."""


def create_job(
    *, query: str, query_lexical: str, ip_hash: str, user_id: str | None = None,
    candidate_ids: list, candidate_meta: list, input_text: str | None = None,
    window_days: int = 90, lexical_hits: int = 0, vector_hits: int = 0,
    vector_ok: bool = False, model: str | None = None, prompt_version: str | None = None,
    request_params: dict | None = None, status: str = "queued", error: str | None = None,
    cap_day: int | None = None, user_day: int | None = None, quota_exempt: bool = False,
) -> str:
    if status != "queued" or error is not None:
        raise ValueError("new jobs must be queued without an error")
    params = dict(
        query=_without_nuls(query), query_lexical=_without_nuls(query_lexical), ip_hash=ip_hash, user_id=user_id,
        candidate_ids=_json(candidate_ids), candidate_meta=_json(candidate_meta),
        input_text=_without_nuls(input_text), window_days=window_days, lexical_hits=lexical_hits,
        vector_hits=vector_hits, vector_ok=vector_ok, model=_without_nuls(model),
        prompt_version=_without_nuls(prompt_version), request_params=_json(request_params), status=status, error=error,
    )
    try:
        with _core.connect() as conn:
            _lock_claims(conn)
            if not quota_exempt and _quota_exhausted_on(conn, user_id, user_day, cap_day):
                raise QuotaExceeded()
            row = conn.execute(
                f"""INSERT INTO {_core.remote_schema()}.ai_search_jobs
                    (query, query_lexical, ip_hash, user_id, candidate_ids, candidate_meta, input_text,
                     window_days, lexical_hits, vector_hits, vector_ok, model, prompt_version,
                     request_params, status, error, write_queued_at)
                    VALUES (%(query)s, %(query_lexical)s, %(ip_hash)s, %(user_id)s, %(candidate_ids)s::jsonb,
                     %(candidate_meta)s::jsonb, %(input_text)s, %(window_days)s, %(lexical_hits)s,
                     %(vector_hits)s, %(vector_ok)s, %(model)s, %(prompt_version)s,
                     %(request_params)s::jsonb, %(status)s, %(error)s, now()) RETURNING id""", params,
            ).fetchone()
    except _core.RemoteDBError as exc:
        # connect() wraps application exceptions after rolling back the transaction.
        if isinstance(exc.__cause__, QuotaExceeded):
            raise exc.__cause__ from None
        raise
    return str(row["id"])


def create_retrieval_job(
    *, query: str, ip_hash: str, scope: dict, user_id: str | None = None,
    user_day: int | None = None, cap_day: int | None = None,
    retrieval_user_day: int = 10, quota_exempt: bool = False,
) -> str:
    """Atomically check writing/attempt limits, then one active retrieval per user.

    Callers supply writing limits as in create_job. Admin/legacy callers must
    pass quota_exempt here and again to finish_retrieval.
    """
    schema = _core.remote_schema()
    try:
        with _core.connect() as conn:
            _lock_claims(conn)
            if not quota_exempt:
                if (_quota_exhausted_on(conn, user_id, user_day, cap_day)
                        or _count_retrieval_day_on(conn, user_id) >= retrieval_user_day):
                    raise QuotaExceeded()
                active = conn.execute(
                    f"SELECT 1 FROM {schema}.ai_search_jobs WHERE user_id = %s AND status='retrieving' LIMIT 1",
                    (user_id,),
                ).fetchone()
                if active:
                    raise RetrievalInProgress()
            row = conn.execute(
                f"""INSERT INTO {schema}.ai_search_jobs
                    (query, ip_hash, user_id, status, candidate_ids, retrieval)
                    VALUES (%(query)s, %(ip_hash)s, %(user_id)s, 'retrieving', '[]'::jsonb,
                            %(retrieval)s::jsonb) RETURNING id""",
                dict(query=_without_nuls(query), ip_hash=ip_hash, user_id=user_id,
                     retrieval=_json({"scope": scope, "stage": "queued"})),
            ).fetchone()
    except _core.RemoteDBError as exc:
        if isinstance(exc.__cause__, (QuotaExceeded, RetrievalInProgress)):
            raise exc.__cause__ from None
        raise
    return str(row["id"])


def _lock_claims(conn: Any) -> None:
    # Serialize the count + claim across workers. The count runs in the NEXT
    # statement (fresh READ COMMITTED snapshot), after the lock is acquired.
    _set_short_statement_timeout(conn, 6000)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_core.remote_schema() + ".ai_search_jobs.claim",))


def claim_job(job_id: str, concurrency: int) -> bool:
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _lock_claims(conn)
        row = conn.execute(
            f"""UPDATE {schema}.ai_search_jobs SET status='running', started_at=now()
                WHERE id=%s AND status='queued'
                  AND (SELECT count(*) FROM {schema}.ai_search_jobs WHERE status='running') < %s
                RETURNING id""", (job_id, int(concurrency)),
        ).fetchone()
    return row is not None


def claim_next_queued(concurrency: int) -> str | None:
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _lock_claims(conn)
        row = conn.execute(
            f"""UPDATE {schema}.ai_search_jobs SET status='running', started_at=now()
                WHERE id=(SELECT id FROM {schema}.ai_search_jobs WHERE status='queued'
                          ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                  AND status='queued'
                  AND (SELECT count(*) FROM {schema}.ai_search_jobs WHERE status='running') < %s
                RETURNING id""", (int(concurrency),),
        ).fetchone()
    return str(row["id"]) if row else None


def claim_next_retrieving(concurrency: int) -> dict | None:
    """Claim FIFO retrieval work; queued retrievals do not consume concurrency."""
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _lock_claims(conn)
        row = conn.execute(
            f"""UPDATE {schema}.ai_search_jobs
                SET retrieval=COALESCE(retrieval, '{{}}'::jsonb)
                              || jsonb_build_object('started_at', now(), 'stage', 'understanding')
                WHERE id=(SELECT id FROM {schema}.ai_search_jobs
                          WHERE status='retrieving' AND NOT (COALESCE(retrieval, '{{}}'::jsonb) ? 'started_at')
                          ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)
                  AND status='retrieving'
                  AND (SELECT count(*) FROM {schema}.ai_search_jobs
                       WHERE status='retrieving' AND retrieval ? 'started_at') < %s
                RETURNING id, query, user_id, retrieval""", (int(concurrency),),
        ).fetchone()
    return {**dict(row), "id": str(row["id"])} if row else None


def update_retrieval_progress(job_id: str, patch: dict) -> None:
    """Merge progress without changing jobs that have left retrieval."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        conn.execute(
            f"""UPDATE {_core.remote_schema()}.ai_search_jobs
                SET retrieval=COALESCE(retrieval, '{{}}'::jsonb) || %s::jsonb
                WHERE id=%s AND status='retrieving'""", (_json(patch), job_id),
        )


def finish_retrieval(
    job_id: str, outcome: str, *, candidate_ids: list, candidate_meta: list,
    retrieval: dict, lexical_hits: int = 0, vector_hits: int = 0,
    vector_ok: bool = False, window_days: int = 90, query_lexical: str | None = None,
    user_day: int | None = None, cap_day: int | None = None, quota_exempt: bool = False,
) -> str | None:
    """Persist sources and atomically charge writing only when it can be queued.

    An insufficient result supplies at most five display candidates. Repeated
    completion returns the current status without overwriting fields; absent
    jobs return None.
    """
    if outcome not in {"ok", "insufficient"}:
        raise ValueError("retrieval outcome must be ok or insufficient")
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _lock_claims(conn)
        job = conn.execute(
            f"SELECT status, user_id FROM {schema}.ai_search_jobs WHERE id=%s FOR UPDATE", (job_id,),
        ).fetchone()
        if not job or job["status"] != "retrieving":
            return job["status"] if job else None
        status, error = "insufficient", None
        if outcome == "ok":
            if not quota_exempt and _quota_exhausted_on(conn, job["user_id"], user_day, cap_day):
                status, error = "failed", "quota_exhausted"
            else:
                status = "queued"
        conn.execute(
            f"""UPDATE {schema}.ai_search_jobs
                SET status=%(status)s, error=%(error)s,
                    write_queued_at=CASE WHEN %(status)s='queued' THEN now() ELSE NULL END,
                    finished_at=CASE WHEN %(status)s='queued' THEN NULL ELSE now() END,
                    candidate_ids=%(candidate_ids)s::jsonb, candidate_meta=%(candidate_meta)s::jsonb,
                    retrieval=COALESCE(retrieval, '{{}}'::jsonb) || %(retrieval)s::jsonb,
                    lexical_hits=%(lexical_hits)s, vector_hits=%(vector_hits)s, vector_ok=%(vector_ok)s,
                    window_days=%(window_days)s, query_lexical=%(query_lexical)s
                WHERE id=%(job_id)s AND status='retrieving'""",
            dict(job_id=job_id, status=status, error=error, candidate_ids=_json(candidate_ids),
                 candidate_meta=_json(candidate_meta), retrieval=_json(retrieval),
                 lexical_hits=lexical_hits, vector_hits=vector_hits, vector_ok=vector_ok,
                 window_days=window_days, query_lexical=_without_nuls(query_lexical)),
        )
    return status


def finish_job(
    job_id: str, *, result: dict, content_md_final: str, cited_ids: list,
    usage: dict, elapsed_ms: int, points_estimate: float, model: str,
    prompt_version: str, request_params: dict,
    input_text: str | None = None, candidate_meta: list | None = None,
) -> None:
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        conn.execute(
            f"""UPDATE {_core.remote_schema()}.ai_search_jobs SET status='done', finished_at=now(),
                result=%(result)s::jsonb, content_md_final=%(content_md_final)s,
                cited_ids=%(cited_ids)s::jsonb, usage=%(usage)s::jsonb, elapsed_ms=%(elapsed_ms)s,
                points_estimate=%(points_estimate)s, model=%(model)s, prompt_version=%(prompt_version)s,
                request_params=%(request_params)s::jsonb, error=NULL,
                input_text=COALESCE(%(input_text)s, input_text),
                candidate_meta=COALESCE(%(candidate_meta)s::jsonb, candidate_meta)
                WHERE id=%(job_id)s AND status='running'""",
            dict(job_id=job_id, result=_json(result), content_md_final=_without_nuls(content_md_final),
                 cited_ids=_json(cited_ids), usage=_json(usage), elapsed_ms=elapsed_ms,
                 points_estimate=points_estimate, model=_without_nuls(model), prompt_version=_without_nuls(prompt_version),
                 request_params=_json(request_params), input_text=_without_nuls(input_text),
                 candidate_meta=_json(candidate_meta)),
        )


def save_job_input(
    job_id: str, *, input_text: str, candidate_meta: list, model: str,
    prompt_version: str, request_params: dict,
) -> None:
    """Persist the exact model input before inference, including failed attempts."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        conn.execute(
            f"""UPDATE {_core.remote_schema()}.ai_search_jobs
                SET input_text=%(input_text)s, candidate_meta=%(candidate_meta)s::jsonb,
                    model=%(model)s, prompt_version=%(prompt_version)s,
                    request_params=%(request_params)s::jsonb
                WHERE id=%(job_id)s AND status='running'""",
            dict(job_id=job_id, input_text=_without_nuls(input_text), candidate_meta=_json(candidate_meta),
                 model=_without_nuls(model), prompt_version=_without_nuls(prompt_version), request_params=_json(request_params)),
        )


def fail_job(job_id: str, error: str, status: str = "failed", raw: str | None = None,
             *, result: dict | None = None) -> None:
    if status not in {"failed", "unavailable"}:
        raise ValueError("failure status must be failed or unavailable")
    result = dict(result) if result is not None else None
    if result is not None and isinstance(result.get('raw'), str):
        result['raw'] = _bounded_raw(result['raw'])
    if raw is not None:
        result = {**(result or {}), 'raw': _bounded_raw(raw)}
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        conn.execute(
            f"""UPDATE {_core.remote_schema()}.ai_search_jobs SET status=%(status)s,
                error=%(error)s, result=COALESCE(%(result)s::jsonb, result), finished_at=now()
                WHERE id=%(job_id)s AND (status IN ('queued', 'running')
                    OR (status='retrieving' AND %(status)s='failed'))""",
            dict(job_id=job_id, status=status, error=_without_nuls(error), result=_json(result)),
        )


def get_job(job_id: str, *, include_input: bool = True) -> dict | None:
    try:
        job_id = str(UUID(str(job_id)))
    except (ValueError, TypeError, AttributeError):
        return None
    columns = "*" if include_input else (
        "id, created_at, started_at, finished_at, status, query, query_lexical, ip_hash, user_id, "
        "window_days, lexical_hits, vector_hits, vector_ok, candidate_ids, candidate_meta, "
        "model, prompt_version, request_params, usage, elapsed_ms, points_estimate, "
        "result, content_md_final, cited_ids, error, write_queued_at, retrieval"
    )
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            f"SELECT {columns} FROM {_core.remote_schema()}.ai_search_jobs WHERE id=%s", (job_id,),
        ).fetchone()
    return dict(row) if row else None


def list_user_jobs(user_id: str, limit: int) -> list[dict]:
    """List one user's recent searches without loading job content or inputs."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        rows = conn.execute(
            f"""SELECT id, query, created_at, status FROM {_core.remote_schema()}.ai_search_jobs
                WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT %s""",
            (user_id, max(1, min(50, int(limit)))),
        ).fetchall()
    return [dict(row) for row in rows]


def count_user_jobs(user_id: str) -> int:
    """Count one user's entire search history, including every job status."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            f"SELECT count(*) AS n FROM {_core.remote_schema()}.ai_search_jobs WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    return int(row["n"])


_BEIJING_DAY_START = "(date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')) AT TIME ZONE 'Asia/Shanghai'"


def _count_day_on(conn: Any, user_id: str | None = None) -> int:
    # now() is fixed at transaction start: an upper bound would exclude jobs
    # from later-started transactions that acquired the advisory lock first.
    row = conn.execute(
        f"""SELECT count(*) AS n FROM {_core.remote_schema()}.ai_search_jobs
            WHERE write_queued_at >= {_BEIJING_DAY_START}"""
        + (" AND user_id = %s" if user_id is not None else
           f" AND user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM {_core.remote_schema()}.users WHERE role = 'admin')"),
        (user_id,) if user_id is not None else (),
    ).fetchone()
    return int(row["n"])


def _count_day(user_id: str | None = None) -> int:
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        return _count_day_on(conn, user_id)


def count_user_day(user_id: str) -> int:
    return _count_day(user_id)


def count_all_day() -> int:
    """Count ordinary accounts' writing entries since Beijing midnight."""
    return _count_day()


def _count_retrieval_day_on(conn: Any, user_id: str | None) -> int:
    row = conn.execute(
        f"""SELECT count(*) AS n FROM {_core.remote_schema()}.ai_search_jobs
            WHERE created_at >= {_BEIJING_DAY_START} AND user_id = %s""", (user_id,),
    ).fetchone()
    return int(row["n"])


def count_user_retrieval_day(user_id: str) -> int:
    """Count the user's submissions today, including v1 and every job state."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        return _count_retrieval_day_on(conn, user_id)


def _quota_exhausted_on(conn: Any, user_id: str | None, user_day: int | None, cap_day: int | None) -> bool:
    return (user_day is not None and _count_day_on(conn, user_id) >= user_day or
            cap_day is not None and _count_day_on(conn) >= cap_day)


def quota_exhausted(user_id: str, user_day: int | None, cap_day: int | None) -> bool:
    """Cheap precheck; create_job repeats it under the shared transaction lock."""
    if user_day is None and cap_day is None:
        return False
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        return _quota_exhausted_on(conn, user_id, user_day, cap_day)


def count_all_month() -> int:
    """Count writing entries this Beijing calendar month, including admins."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            f"""SELECT count(*) AS n FROM {_core.remote_schema()}.ai_search_jobs
                WHERE write_queued_at >= date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'""",
        ).fetchone()
    return int(row["n"])


def queue_position(job_id: str) -> int:
    """One-based queue rank; zero if the target is no longer queued."""
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            f"""SELECT count(*) AS n FROM {schema}.ai_search_jobs older
                JOIN {schema}.ai_search_jobs target ON target.id=%s AND target.status='queued'
                WHERE older.status='queued' AND (older.created_at, older.id) <= (target.created_at, target.id)""",
            (job_id,),
        ).fetchone()
    return int(row["n"])


def recover_stale(hard_timeout_minutes: int = 45, *, requeue_before: datetime | None = None) -> None:
    """Expire timeouts; only requeue jobs predating the fixed process start."""
    schema = _core.remote_schema()
    with _core.connect() as conn:
        _lock_claims(conn)
        conn.execute(
            f"""UPDATE {schema}.ai_search_jobs SET status='failed', error='hard_timeout', finished_at=now()
                WHERE status='running' AND started_at < now() - make_interval(mins => %s)""",
            (int(hard_timeout_minutes),),
        )
        if requeue_before is not None:
            conn.execute(
                f"""UPDATE {schema}.ai_search_jobs SET status='queued', started_at=NULL, finished_at=NULL
                    WHERE status='running' AND started_at < %s""", (requeue_before,),
            )
        conn.execute(
            f"""UPDATE {schema}.ai_search_jobs SET status='failed', error='retrieval_timeout', finished_at=now()
                WHERE status='retrieving' AND created_at < now() - interval '5 minutes'""",
        )
        if requeue_before is not None:
            conn.execute(
                f"""UPDATE {schema}.ai_search_jobs
                    SET retrieval=(retrieval - 'started_at') || '{{"stage": "queued"}}'::jsonb
                    WHERE status='retrieving' AND (retrieval->>'started_at')::timestamptz < %s""",
                (requeue_before,),
            )


def list_jobs(limit: int, offset: int, status: str | None = None) -> list:
    params = ((status,) if status else ()) + (max(0, int(limit)), max(0, int(offset)))
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        rows = conn.execute(
            f"""SELECT id, created_at, started_at, finished_at, status, query, query_lexical,
                window_days, lexical_hits, vector_hits, vector_ok,
                jsonb_array_length(candidate_ids) AS candidate_count,
                elapsed_ms, usage, points_estimate, model, prompt_version, error
                FROM {_core.remote_schema()}.ai_search_jobs"""
            + (" WHERE status = %s" if status else "")
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", params,
        ).fetchall()
    return [dict(row) for row in rows]


def count_jobs(status: str | None = None) -> int:
    """Count all matching jobs, without the list endpoint's pagination."""
    with _core.connect() as conn:
        _set_short_statement_timeout(conn, 6000)
        row = conn.execute(
            f"SELECT count(*) AS n FROM {_core.remote_schema()}.ai_search_jobs"
            + (" WHERE status = %s" if status else ""),
            (status,) if status else (),
        ).fetchone()
    return int(row["n"])


def points_estimate(usage: dict) -> float:
    cached = usage.get("cached_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return ((usage.get("prompt_tokens") or 0) * 6.9 + cached * 1.7 + (usage.get("completion_tokens") or 0) * 24) / 10000
