from __future__ import annotations


def events_read_from_remote() -> bool:
    return event_read_backend() in REMOTE_BACKENDS


def feed_read_from_remote() -> bool:
    return feed_read_backend() in REMOTE_BACKENDS


def _feed_first_paint_per_group(env: dict[str, str] | None = None) -> int:
    # 与 routes/feed.py 的首屏每组条数同一 env/边界(默认 20,clamp 5..100)。
    # prewarm 按此值预热 sections/platforms,才能和路由首屏请求命中同一缓存 key。
    return min(
        max(
            _env_int(
                env or _runtime_env(),
                "INFO2ACTION_FEED_FIRST_PAINT_PER_GROUP",
                20,
                min_value=1,
            ),
            5,
        ),
        100,
    )


def _feed_result_cache_ttl_sec(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    return _env_int(values, FEED_RESULT_CACHE_TTL_ENV, 900, min_value=0)


def _remote_snapshot_ttl(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    return _env_int(values, REMOTE_SNAPSHOT_TTL_ENV, 1800, min_value=0)
_FEED_SNAPSHOT_KEY_PREFIXES = ("events:", "sections:")

_DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES = -480
_MAX_TIMEZONE_OFFSET_MINUTES = 14 * 60


def _timezone_offset_minutes(value: int | None) -> int:
    try:
        offset = int(value if value is not None else _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES)
    except (TypeError, ValueError):
        offset = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES
    return max(-_MAX_TIMEZONE_OFFSET_MINUTES, min(_MAX_TIMEZONE_OFFSET_MINUTES, offset))


def clear_feed_snapshot_rows(prefixes: tuple[str, ...] = _FEED_SNAPSHOT_KEY_PREFIXES) -> int:
    """Remove Supabase feed read-model snapshots after visible feed data changes."""
    if not prefixes:
        return 0
    conditions = " OR ".join(["snapshot_key LIKE %s"] * len(prefixes))
    params = tuple(f"{prefix}%" for prefix in prefixes)
    try:
        with connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {remote_schema()}.feed_snapshots WHERE {conditions}",
                params,
            )
            conn.commit()
            return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        return 0


def _feed_snapshots_available(conn: Any, schema: str) -> bool:
    cache_key = ("feed_snapshots_available", schema)
    cached = _cache_get_with_ttl(cache_key, 300)
    if cached is not None:
        return bool(cached)
    try:
        row = conn.execute("select to_regclass(%s) as name", (f"{schema}.feed_snapshots",)).fetchone()
        available = bool(row and row.get("name"))
    except Exception:
        _rollback_safely(conn)
        available = False
    _cache_set_with_ttl(cache_key, available, 300)
    return available


def refresh_info_pill_counts() -> dict[str, Any]:
    """perf-v27 P4: 每次抓取后重算各 pill 的全量计数快照(目标架构定稿 §0-6)。

    读模型收缩到 7 天热窗口后,scopes.total_count 只反映 7 天量;而信息页
    模块表头的「N 条」按产品决策要显示全保留期(90 天)总量。这里在
    post-fetch 一次性 GROUP BY 出快照落表,读路径只查小表——绝不在请求时
    对大表实时 count(count(*) 是本库的经典性能陷阱)。
    当前只算 section_category(信息默认页表头);其余 pill 维度按需扩展。
    """
    if not remote_authority_enabled():
        return {"ok": True, "skipped": "not_remote"}
    schema = remote_schema()
    section_category_expr = _section_category_expr("i")
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=INFO_READ_MODEL_MIN_GITHUB_STARS,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where_sql = _where_sql(where)
    t0 = time.time()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 30000)
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {schema}.info_pill_counts (
                       kind text NOT NULL,
                       value text NOT NULL,
                       n integer NOT NULL,
                       computed_at timestamptz NOT NULL DEFAULT now(),
                       PRIMARY KEY (kind, value)
                     )"""
            )
            conn.execute(
                f"""WITH counts AS (
                       SELECT {section_category_expr} AS value, count(*)::integer AS n
                         FROM {schema}.items i
                         {where_sql}
                        GROUP BY 1
                     ),
                     upserted AS (
                       INSERT INTO {schema}.info_pill_counts (kind, value, n, computed_at)
                       SELECT 'section_category', value, n, now() FROM counts
                       ON CONFLICT (kind, value) DO UPDATE SET
                         n = excluded.n,
                         computed_at = excluded.computed_at
                       RETURNING value
                     )
                     DELETE FROM {schema}.info_pill_counts pc
                      WHERE pc.kind = 'section_category'
                        AND pc.value NOT IN (SELECT value FROM upserted)""",
                params,
            )
            _commit_safely(conn)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "elapsed_ms": int((time.time() - t0) * 1000)}
    return {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}


def _info_pill_counts_overlay(conn: Any, schema: str, kind: str) -> dict[str, int]:
    """读取计数快照;快照缺席/失败时返回空 dict(调用方回退到 scopes 计数)。"""
    try:
        rows = conn.execute(
            f"SELECT value, n FROM {schema}.info_pill_counts WHERE kind = %(kind)s",
            {"kind": kind},
        ).fetchall()
    except Exception:
        _rollback_safely(conn)
        return {}
    return {str(r["value"]): int(r["n"]) for r in (dict(row) for row in rows) if r.get("value")}


def _info_read_model_freshness(
    conn: Any,
    schema: str,
    *,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    active = _info_read_model_active_version(conn, schema)
    active_meta = _json_value(active.get("meta_json")) if active else {}
    if not isinstance(active_meta, dict):
        active_meta = {}
    active_sort_policy = active_meta.get("sort_policy") if active else None
    sort_policy_stale = active_sort_policy != INFO_READ_MODEL_SORT_POLICY
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=int(min_github_stars),
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    latest = conn.execute(
        f"""SELECT i.fetched_at AS latest_fetched_at
              FROM {schema}.items i
              {_where_sql(where)}
             ORDER BY i.fetched_at DESC NULLS LAST
             LIMIT 1""",
        params,
    ).fetchone()
    active_max = active.get("max_fetched_at") if active else None
    latest_max = (latest or {}).get("latest_fetched_at")
    result = {
        "active_version_id": str(active.get("version_id")) if active and active.get("version_id") else None,
        "active_generated_at": _timestamp_value(active.get("generated_at")) if active else None,
        "active_max_fetched_at": _timestamp_value(active_max),
        "latest_max_fetched_at": _timestamp_value(latest_max),
        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
        "active_sort_policy": active_sort_policy,
        "sort_policy_stale": bool(sort_policy_stale),
        "data_stale": bool(
            latest_max
            and (
                not active_max
                or sort_key(latest_max) > sort_key(active_max)
            )
        ),
    }
    result["stale"] = bool(result["sort_policy_stale"] or result["data_stale"])
    return result


def info_read_model_freshness_remote(
    *,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Read-only freshness probe for the Info tab read model."""
    enabled = _info_read_model_enabled()
    result: dict[str, Any] = {
        "enabled": enabled,
        "read_model": "info_platforms_v1",
        "state_key": INFO_READ_MODEL_STATE_KEY,
        "data_backend": feed_read_backend(),
        "incremental_enabled": _info_read_model_incremental_enabled(),
    }
    if not enabled:
        return result
    schema = remote_schema()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            result.update(
                _info_read_model_freshness(
                    conn,
                    schema=schema,
                    min_github_stars=min_github_stars,
                )
            )
    except Exception as exc:
        raise RemoteDBError(f"info read model freshness probe failed: {exc}") from exc
    return result


def _info_read_model_incremental_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), INFO_READ_MODEL_INCREMENTAL_ENV, default=True)


def _info_read_model_scope_rows_select(source_table: str) -> str:
    # perf-v27 P4: 只保留 section_category 维度(信息默认页 = 每模块「全部」
    # pill 首屏)。原 5 维度乘法式物化(all/source/group/category×2)全部下线,
    # 对应视图改走 live 现场查——查询路由的既有 None→live 回退自动接管。
    # (ENG-0710 砍 section_subcategory/group_source 的延续与收口。)
    return f"""WITH raw_scope_rows AS (
                      SELECT '_all'::text AS platform, 'section_category'::text AS dimension,
                             i.section_category AS value,
                             i.id::text AS item_id, i.sort_at, i.fetched_at, i.relevance_score
                        FROM {source_table} i
                   )
            SELECT 'platform=' || platform || '|dimension=' || dimension || '|value=' || value AS scope_key,
                   platform, dimension, value, item_id, sort_at, fetched_at, relevance_score,
                   sort_at AS rank_at
              FROM raw_scope_rows"""


def _info_scope_item_order_sql(alias: str = "si") -> str:
    return (
        f"{alias}.sort_at DESC NULLS LAST, "
        f"{alias}.fetched_at DESC NULLS LAST, "
        f"{alias}.relevance_score DESC NULLS LAST, "
        f"{alias}.item_id DESC"
    )


def _info_read_model_delta_window_hours(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env if env is not None else _runtime_env(),
        INFO_READ_MODEL_DELTA_WINDOW_HOURS_ENV,
        INFO_READ_MODEL_DELTA_WINDOW_HOURS_DEFAULT,
        min_value=1,
    )


def _delta_window_bounds(min_start: Any, now_ts: Any, window_hours: int) -> tuple[Any, bool]:
    """一轮追赶窗口的上界:min(min_start+窗口, now)。触到 now 即视为追平。"""
    window_end = min_start + timedelta(hours=int(window_hours))
    if now_ts is not None and window_end >= now_ts:
        return now_ts, True
    return window_end, False


def refresh_info_read_model_delta_in_place(
    *,
    sample_limit: int = 200,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Apply new info items to the active read model without cloning all rows.

    BF-0710-1: delta 按时间窗分轮追赶。每轮一个独立事务、独立提交,失败只丢当轮;
    statement_timeout 随剩余墙钟预算收缩,预算耗尽带已提交进度收官(caught_up=False),
    由下一次调度接力。长积压不再是一条全有或全无的巨型 SQL。
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )
    window_hours = _info_read_model_delta_window_hours()

    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _info_read_model_active_version(conn, schema)
    if not active or not active.get("version_id") or not active.get("max_fetched_at"):
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_meta = _json_value(active.get("meta_json"))
    if not isinstance(active_meta, dict) or active_meta.get("sort_policy") != INFO_READ_MODEL_SORT_POLICY:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    # perf-v27 P4: 版本形态自愈——active 版本还是旧乘法式形态(全维度/无窗口/
    # 无帽)时直接全量重建换版,绝不在 313k 行旧版本上跑 delta 的 prune。
    if active_meta.get("scope_profile") != INFO_READ_MODEL_SCOPE_PROFILE:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    current_watermark = active["max_fetched_at"]
    timings_ms["read_active_version"] = int((time.time() - step_t0) * 1000)

    deadline = t0 + refresh_timeout_ms / 1000.0
    rounds = 0
    total_delta = 0
    caught_up = False
    budget_exhausted = False
    max_rounds_hit = False
    while True:
        remaining_ms = int((deadline - time.time()) * 1000)
        if rounds and remaining_ms <= _INFO_READ_MODEL_DELTA_ROUND_FLOOR_MS:
            budget_exhausted = True
            break
        round_result = _refresh_info_read_model_delta_round(
            schema=schema,
            active_version_id=active_version_id,
            watermark=current_watermark,
            min_github_stars=safe_min_github_stars,
            window_hours=window_hours,
            statement_timeout_ms=min(refresh_timeout_ms, max(remaining_ms, 60000)),
        )
        for step, ms in (round_result.get("timings_ms") or {}).items():
            timings_ms[step] = timings_ms.get(step, 0) + ms
        if round_result.get("status") == "no_delta":
            caught_up = True
            if not rounds:
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": _timestamp_value(current_watermark),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            break
        rounds += 1
        total_delta += int(round_result.get("delta_items") or 0)
        current_watermark = round_result.get("max_fetched_at") or current_watermark
        if round_result.get("reached_now"):
            caught_up = True
            break
        if rounds >= _INFO_READ_MODEL_DELTA_MAX_ROUNDS:
            max_rounds_hit = True
            break
    if total_delta > 0:
        clear_feed_cache_keys()
    result: dict[str, Any] = {
        "ok": True,
        "mode": "delta_in_place",
        "version_id": active_version_id,
        "delta_items": total_delta,
        "rounds": rounds,
        "caught_up": caught_up,
        "window_hours": window_hours,
        "active_max_fetched_at": _timestamp_value(current_watermark),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }
    # 有界收官不是失败,但要在结果里说清什么没做完(交给下一次调度接力)。
    if budget_exhausted:
        result["budget_exhausted"] = True
    if max_rounds_hit:
        result["max_rounds_hit"] = True
    return result


def _refresh_info_read_model_delta_round(
    *,
    schema: str,
    active_version_id: str,
    watermark: Any,
    min_github_stars: int,
    window_hours: int,
    statement_timeout_ms: int,
) -> dict[str, Any]:
    """One bounded catch-up round over (watermark, min_start+window] in a single tx.

    水位(update_active_version)只推进到本轮实际物化的 max(fetched_at),
    绝不推到窗口上界;空窗口不动水位(宁可少推进不可越过,P0-1 checkpoint 语义)。
    """
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=int(min_github_stars),
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where.append("i.fetched_at > %(active_max_fetched_at)s::timestamptz")
    probe_where_sql = _where_sql(where)
    where.append("i.fetched_at <= %(delta_window_end)s::timestamptz")
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    delta_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    timings_ms: dict[str, int] = {}
    current_step = "init"

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = timings_ms.get(step, 0) + int((time.time() - started_at) * 1000)

    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, statement_timeout_ms)
            current_step = "probe_delta_window"
            step_t0 = time.time()
            probe = conn.execute(
                f"""SELECT i.fetched_at AS min_start, now() AS now_ts
                      FROM {schema}.items i
                      {probe_where_sql}
                     ORDER BY i.fetched_at ASC
                     LIMIT 1""",
                {**params, "active_max_fetched_at": watermark},
            ).fetchone()
            min_start = (probe or {}).get("min_start")
            now_ts = (probe or {}).get("now_ts")
            if min_start is None:
                conn.commit()
                _record_step(current_step, step_t0)
                return {"status": "no_delta", "timings_ms": timings_ms}
            delta_window_end, reached_now = _delta_window_bounds(min_start, now_ts, window_hours)
            _record_step(current_step, step_t0)

            current_step = "materialize_delta"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta ON COMMIT DROP AS
                    {delta_select_sql}""",
                {
                    **params,
                    "active_max_fetched_at": watermark,
                    "delta_window_end": delta_window_end,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta")
            delta_row = conn.execute(
                "SELECT count(*) AS n, max(fetched_at) AS max_fetched_at FROM pg_temp.info_read_model_delta"
            ).fetchone()
            delta_count = int((delta_row or {}).get("n") or 0)
            if delta_count <= 0:
                # 竞态兜底:探针可见但物化为空 —— 当无增量处理,水位不动。
                conn.commit()
                _record_step(current_step, step_t0)
                return {"status": "no_delta", "timings_ms": timings_ms}
            delta_max_fetched_at = (delta_row or {}).get("max_fetched_at")
            _record_step(current_step, step_t0)

            current_step = "upsert_delta_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', left(i.ai_summary, 280),
                              'ai_category', i.ai_category,
                              'ai_categories', i.ai_categories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_delta i
                     ON CONFLICT (version_id, item_id) DO UPDATE SET
                       card_json = excluded.card_json,
                       platform = excluded.platform,
                       source = excluded.source,
                       sort_at = excluded.sort_at,
                       fetched_at = excluded.fetched_at,
                       published_at = excluded.published_at,
                       relevance_score = excluded.relevance_score
                     WHERE {schema}.info_card_items.card_json IS DISTINCT FROM excluded.card_json
                        OR {schema}.info_card_items.platform IS DISTINCT FROM excluded.platform
                        OR {schema}.info_card_items.source IS DISTINCT FROM excluded.source
                        OR {schema}.info_card_items.sort_at IS DISTINCT FROM excluded.sort_at
                        OR {schema}.info_card_items.fetched_at IS DISTINCT FROM excluded.fetched_at
                        OR {schema}.info_card_items.published_at IS DISTINCT FROM excluded.published_at
                        OR {schema}.info_card_items.relevance_score IS DISTINCT FROM excluded.relevance_score""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta_scope_rows ON COMMIT DROP AS
                    {_info_read_model_scope_rows_select("pg_temp.info_read_model_delta")}""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_existing_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_existing_delta_scope_rows ON COMMIT DROP AS
                    SELECT sc.scope_key, sc.platform, sc.dimension, sc.value,
                           si.item_id, si.sort_at, si.fetched_at, si.relevance_score,
                           si.sort_at AS rank_at
                      FROM {schema}.info_scope_items si
                      JOIN {schema}.info_scopes sc
                        ON sc.version_id = si.version_id
                       AND sc.scope_key = si.scope_key
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta d
                              WHERE d.id::text = si.item_id
                           )""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_existing_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scopes")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_delta_scope_rows
                    UNION
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_existing_delta_scope_rows"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "delete_obsolete_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""DELETE FROM {schema}.info_scope_items si
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta d
                              WHERE d.id::text = si.item_id
                           )
                       AND NOT EXISTS (
                             SELECT 1
                               FROM pg_temp.info_read_model_delta_scope_rows dsr
                              WHERE dsr.scope_key = si.scope_key
                                AND dsr.item_id = si.item_id
                           )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "update_existing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_scope_items si
                       SET sort_at = dsr.sort_at,
                           fetched_at = dsr.fetched_at,
                           relevance_score = dsr.relevance_score
                      FROM pg_temp.info_read_model_delta_scope_rows dsr
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND si.scope_key = dsr.scope_key
                       AND si.item_id = dsr.item_id
                       AND (
                            si.sort_at IS DISTINCT FROM dsr.sort_at
                         OR si.fetched_at IS DISTINCT FROM dsr.fetched_at
                         OR si.relevance_score IS DISTINCT FROM dsr.relevance_score
                       )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_missing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH missing AS (
                       SELECT dsr.scope_key, dsr.item_id, dsr.sort_at, dsr.fetched_at,
                              dsr.relevance_score, dsr.rank_at
                         FROM pg_temp.info_read_model_delta_scope_rows dsr
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM {schema}.info_scope_items si
                                 WHERE si.version_id = %(active_version_id)s::uuid
                                   AND si.scope_key = dsr.scope_key
                                   AND si.item_id = dsr.item_id
                              )
                     ),
                     scope_max_rank AS (
                       SELECT si.scope_key, max(si.rank) AS max_rank
                         FROM {schema}.info_scope_items si
                        WHERE si.version_id = %(active_version_id)s::uuid
                          AND EXISTS (
                                SELECT 1
                                  FROM pg_temp.info_read_model_affected_scopes a
                                 WHERE a.scope_key = si.scope_key
                              )
                        GROUP BY si.scope_key
                     ),
                     ranked AS (
                       SELECT m.scope_key, m.item_id, m.sort_at, m.fetched_at,
                              m.relevance_score,
                              COALESCE(smr.max_rank, 0) + row_number() OVER (
                                PARTITION BY m.scope_key
                                ORDER BY m.rank_at DESC NULLS LAST,
                                         m.fetched_at DESC NULLS LAST,
                                         m.relevance_score DESC NULLS LAST,
                                         m.item_id DESC
                              ) AS append_rank
                         FROM missing m
                         LEFT JOIN scope_max_rank smr
                           ON smr.scope_key = m.scope_key
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid, scope_key, append_rank::integer,
                            item_id, sort_at, fetched_at, relevance_score
                       FROM ranked""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "upsert_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_meta")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_delta_scope_meta ON COMMIT DROP AS
                   SELECT scope_key, max(platform) AS platform, max(dimension) AS dimension,
                          max(value) AS value
                     FROM pg_temp.info_read_model_delta_scope_rows
                    GROUP BY scope_key"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_meta")
            conn.execute(
                f"""WITH affected_scope_stats AS (
                       SELECT a.scope_key,
                              COALESCE(max(dsm.platform), max(sc.platform)) AS platform,
                              COALESCE(max(dsm.dimension), max(sc.dimension)) AS dimension,
                              COALESCE(max(dsm.value), max(sc.value), '') AS value,
                              count(si.item_id)::integer AS total_count,
                              max(si.sort_at) AS max_sort_at
                         FROM pg_temp.info_read_model_affected_scopes a
                         LEFT JOIN {schema}.info_scopes sc
                           ON sc.version_id = %(active_version_id)s::uuid
                          AND sc.scope_key = a.scope_key
                         LEFT JOIN pg_temp.info_read_model_delta_scope_meta dsm
                           ON dsm.scope_key = a.scope_key
                         LEFT JOIN {schema}.info_scope_items si
                           ON si.version_id = %(active_version_id)s::uuid
                          AND si.scope_key = a.scope_key
                        GROUP BY a.scope_key
                     )
                     INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            platform,
                            dimension,
                            value,
                            total_count,
                            max_sort_at,
                            now()
                       FROM affected_scope_stats
                      WHERE total_count > 0
                     ON CONFLICT (version_id, scope_key) DO UPDATE SET
                       platform = excluded.platform,
                       dimension = excluded.dimension,
                       value = excluded.value,
                       total_count = excluded.total_count,
                       max_sort_at = excluded.max_sort_at,
                       generated_at = excluded.generated_at""",
                {"active_version_id": active_version_id},
            )
            conn.execute(
                f"""DELETE FROM {schema}.info_scopes sc
                      USING pg_temp.info_read_model_affected_scopes a
                     WHERE sc.version_id = %(active_version_id)s::uuid
                       AND sc.scope_key = a.scope_key
                       AND NOT EXISTS (
                             SELECT 1
                               FROM {schema}.info_scope_items si
                              WHERE si.version_id = sc.version_id
                                AND si.scope_key = sc.scope_key
                           )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            # perf-v27 P4: 收口热窗口与封顶——delta 只增不减,不修剪的话
            # in-place 版本会随时间越出 7 天窗口/每 scope TOP_N 帽。
            # 在封顶后的小表(~700 行)上这三条 DELETE 均为瞬时。
            current_step = "prune_window_and_cap"
            step_t0 = time.time()
            conn.execute(
                f"""DELETE FROM {schema}.info_scope_items
                     WHERE version_id = %(active_version_id)s::uuid
                       AND fetched_at < now() - (%(info_window_days)s::int * interval '1 day')""",
                {
                    "active_version_id": active_version_id,
                    "info_window_days": INFO_READ_MODEL_WINDOW_DAYS,
                },
            )
            conn.execute(
                f"""DELETE FROM {schema}.info_scope_items si
                     USING (
                       SELECT scope_key, item_id,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY {_info_scope_item_order_sql("inner_si")}
                              ) AS rn
                         FROM {schema}.info_scope_items inner_si
                        WHERE inner_si.version_id = %(active_version_id)s::uuid
                     ) overflow
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND si.scope_key = overflow.scope_key
                       AND si.item_id = overflow.item_id
                       AND overflow.rn > %(scope_top_n)s""",
                {
                    "active_version_id": active_version_id,
                    "scope_top_n": INFO_READ_MODEL_SCOPE_TOP_N,
                },
            )
            conn.execute(
                f"""DELETE FROM {schema}.info_card_items ci
                     WHERE ci.version_id = %(active_version_id)s::uuid
                       AND NOT EXISTS (
                         SELECT 1 FROM {schema}.info_scope_items si
                          WHERE si.version_id = ci.version_id
                            AND si.item_id = ci.item_id
                       )""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "update_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET max_fetched_at = GREATEST(
                             COALESCE(max_fetched_at, '-infinity'::timestamptz),
                             %(delta_max_fetched_at)s::timestamptz
                           ),
                           completed_at = now(),
                           meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'sort_policy', %(sort_policy)s::text,
                             'last_delta_mode', 'in_place',
                             'last_delta_at', now()
                           )
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    "active_version_id": active_version_id,
                    "delta_max_fetched_at": delta_max_fetched_at,
                    "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                },
            )
            conn.execute(
                f"""UPDATE {schema}.info_read_model_state
                       SET updated_at = now()
                     WHERE key = %(state_key)s
                       AND active_version_id = %(active_version_id)s::uuid""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "active_version_id": active_version_id,
                },
            )
            _record_step(current_step, step_t0)

            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"info read model in-place delta refresh failed at {current_step}: {exc}") from exc
    return {
        "status": "applied",
        "delta_items": delta_count,
        "max_fetched_at": delta_max_fetched_at,
        "reached_now": reached_now,
        "timings_ms": timings_ms,
    }


def _info_read_model_refresh_effective_interval(min_interval_sec: int, consecutive_failures: int) -> int:
    """BF-0710-1 失败指数退避:min_interval × 2^连续失败次数,封顶 2h。

    无退避的固定间隔重试是把偶发拥塞滚成死循环的发动机(BF-0708 系列 P0):
    刷新失败→回滚→10min 后面对同样(略多)的 delta 原样重来,期间把库 IO 吃满。
    BF-0710-7 起同时服务 info / highlights / platforms-MV 三处 *_if_stale 守卫。
    """
    if consecutive_failures <= 0:
        return min_interval_sec
    cap = max(int(min_interval_sec), INFO_READ_MODEL_REFRESH_BACKOFF_CAP_SEC)
    return int(min(min_interval_sec * (2 ** min(consecutive_failures, 16)), cap))


def refresh_info_read_model_if_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    global _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT, _INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    min_interval = max(0, int(min_interval_sec))
    now = time.monotonic()
    with _INFO_READ_MODEL_REFRESH_LOCK:
        failures = _INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES
        effective_interval = _info_read_model_refresh_effective_interval(min_interval, failures)
        age = now - _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT if _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT else None
        if age is not None and age < effective_interval:
            skipped: dict[str, Any] = {
                "ok": True,
                "skipped": "recent_attempt",
                "age_sec": round(age, 1),
                "min_interval_sec": min_interval,
            }
            if failures:
                skipped["consecutive_failures"] = failures
                skipped["effective_interval_sec"] = effective_interval
            return skipped
        _INFO_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = now
    try:
        schema = remote_schema()
        with connect() as conn:
            _set_short_statement_timeout(conn, 15000)
            freshness = _info_read_model_freshness(conn, schema=schema)
        if not freshness.get("stale"):
            result: dict[str, Any] = {
                "ok": True,
                "skipped": "data_fresh",
                **freshness,
            }
        elif freshness.get("sort_policy_stale"):
            migration_result = migrate_info_read_model_sort_policy()
            if freshness.get("data_stale") and _info_read_model_incremental_enabled():
                result = refresh_info_read_model_delta_in_place()
                result["sort_policy_migration"] = migration_result
            else:
                result = migration_result
        elif _info_read_model_incremental_enabled():
            result = refresh_info_read_model_delta_in_place()
        else:
            result = refresh_info_read_model()
    except Exception:
        with _INFO_READ_MODEL_REFRESH_LOCK:
            _INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES += 1
        raise
    with _INFO_READ_MODEL_REFRESH_LOCK:
        _INFO_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES = 0
    return result


def refresh_info_read_model_incremental(
    *,
    sample_limit: int = 200,
    min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Create a new complete info read-model version by re-ranking only delta scopes.

    The reader contract stays simple: every served page still points at a complete
    version. The builder avoids re-scanning historical items; it clones the active
    projection, materializes items newer than the active max, and re-ranks only the
    scopes touched by that delta.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=safe_min_github_stars,
    )
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    where.append("i.fetched_at > %(active_max_fetched_at)s::timestamptz")
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    delta_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    active_version_id: str | None = None
    active_max_fetched_at: Any = None
    delta_count = 0
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    current_step = "read_active_version"
    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _info_read_model_active_version(conn, schema)
    if not active or not active.get("version_id") or not active.get("max_fetched_at"):
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_meta = _json_value(active.get("meta_json"))
    if not isinstance(active_meta, dict) or active_meta.get("sort_policy") != INFO_READ_MODEL_SORT_POLICY:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    # perf-v27 P4: 版本形态自愈——active 版本还是旧乘法式形态(全维度/无窗口/
    # 无帽)时直接全量重建换版,绝不在 313k 行旧版本上跑 delta 的 prune。
    if active_meta.get("scope_profile") != INFO_READ_MODEL_SCOPE_PROFILE:
        return refresh_info_read_model(
            sample_limit=safe_sample_limit,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    active_max_fetched_at = active["max_fetched_at"]
    _record_step(current_step, step_t0)

    with connect() as conn:
        # BF-0706-4: 单飞锁 —— 已有重建在跑就跳过,避免并发叠加压崩 compute。
        got_lock = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (_INFO_READ_MODEL_BUILD_LOCK_KEY,)
        ).fetchone()["locked"]
        if not got_lock:
            return {"ok": True, "skipped": "build_in_progress"}
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "create_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_versions (
                       version_id, status, generated_at, sample_limit, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(sample_limit)s,
                       jsonb_build_object(
                         'min_github_stars', %(min_github_stars)s::integer,
                         'mode', 'incremental',
                         'parent_version_id', %(active_version_id)s::text,
                         'from_fetched_at', %(active_max_fetched_at)s::text,
                         'sort_policy', %(sort_policy)s::text
                       )
                     )""",
                {
                    "version_id": version_id,
                    "sample_limit": safe_sample_limit,
                    "min_github_stars": safe_min_github_stars,
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": active_max_fetched_at,
                    "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                },
            )
            conn.commit()
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            _record_step(current_step, step_t0)

            current_step = "materialize_delta"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta ON COMMIT DROP AS
                    {delta_select_sql}""",
                {**params, "active_max_fetched_at": active_max_fetched_at},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta")
            delta_row = conn.execute(
                "SELECT count(*) AS n, max(fetched_at) AS max_fetched_at FROM pg_temp.info_read_model_delta"
            ).fetchone()
            delta_count = int((delta_row or {}).get("n") or 0)
            if delta_count <= 0:
                conn.execute(
                    f"DELETE FROM {schema}.info_read_model_versions WHERE version_id = %(version_id)s::uuid",
                    {"version_id": version_id},
                )
                conn.commit()
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "active_version_id": active_version_id,
                    "active_max_fetched_at": _timestamp_value(active_max_fetched_at),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            delta_max_fetched_at = (delta_row or {}).get("max_fetched_at")
            _record_step(current_step, step_t0)

            current_step = "clone_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, ci.item_id, ci.card_json,
                            ci.platform, ci.source, ci.sort_at, ci.fetched_at,
                            ci.published_at, ci.relevance_score
                       FROM {schema}.info_card_items ci
                      WHERE ci.version_id = %(active_version_id)s::uuid""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_delta_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', left(i.ai_summary, 280),
                              'ai_category', i.ai_category,
                              'ai_categories', i.ai_categories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_delta i
                     ON CONFLICT (version_id, item_id) DO UPDATE SET
                       card_json = excluded.card_json,
                       platform = excluded.platform,
                       source = excluded.source,
                       sort_at = excluded.sort_at,
                       fetched_at = excluded.fetched_at,
                       published_at = excluded.published_at,
                       relevance_score = excluded.relevance_score""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_delta_scope_rows ON COMMIT DROP AS
                    {_info_read_model_scope_rows_select("pg_temp.info_read_model_delta")}""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_delta_scope_rows")
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scopes")
            conn.execute(
                """CREATE TEMP TABLE info_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.info_read_model_delta_scope_rows"""
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "copy_unaffected_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid, sc.scope_key, sc.platform,
                            sc.dimension, sc.value, sc.total_count,
                            sc.max_sort_at, now()
                       FROM {schema}.info_scopes sc
                      WHERE sc.version_id = %(active_version_id)s::uuid
                        AND NOT EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                         WHERE a.scope_key = sc.scope_key)""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            conn.execute(
                f"""INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id,
                       sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, si.scope_key, si.rank,
                            si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                       FROM {schema}.info_scope_items si
                      WHERE si.version_id = %(active_version_id)s::uuid
                        AND NOT EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                         WHERE a.scope_key = si.scope_key)""",
                {"version_id": version_id, "active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_affected_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_affected_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_affected_scope_rows ON COMMIT DROP AS
                    SELECT sc.scope_key, sc.platform, sc.dimension, sc.value,
                           si.item_id, si.sort_at, si.fetched_at, si.relevance_score,
                           si.sort_at AS rank_at
                      FROM {schema}.info_scope_items si
                      JOIN {schema}.info_scopes sc
                        ON sc.version_id = si.version_id
                       AND sc.scope_key = si.scope_key
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (SELECT 1 FROM pg_temp.info_read_model_affected_scopes a
                                    WHERE a.scope_key = si.scope_key)
                    UNION ALL
                    SELECT scope_key, platform, dimension, value, item_id,
                           sort_at, fetched_at, relevance_score, rank_at
                      FROM pg_temp.info_read_model_delta_scope_rows""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_affected_scope_rows")
            _record_step(current_step, step_t0)

            current_step = "insert_affected_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""WITH deduped AS (
                       SELECT *,
                              row_number() OVER (
                                PARTITION BY scope_key, item_id
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS item_rn
                         FROM pg_temp.info_read_model_affected_scope_rows
                     )
                     INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            max(platform) AS platform,
                            max(dimension) AS dimension,
                            max(value) AS value,
                            count(*)::integer,
                            max(rank_at),
                            now()
                       FROM deduped
                      WHERE item_rn = 1
                      GROUP BY scope_key""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "insert_affected_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH deduped AS (
                       SELECT *,
                              row_number() OVER (
                                PARTITION BY scope_key, item_id
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS item_rn
                         FROM pg_temp.info_read_model_affected_scope_rows
                     ),
                     ranked AS (
                       SELECT scope_key, item_id, sort_at, fetched_at, relevance_score,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS rn
                         FROM deduped
                        WHERE item_rn = 1
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, scope_key, rn::integer, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM ranked""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "complete_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_fetched_at = GREATEST(
                             %(active_max_fetched_at)s::timestamptz,
                             %(delta_max_fetched_at)s::timestamptz
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {
                    "version_id": version_id,
                    "active_max_fetched_at": active_max_fetched_at,
                    "delta_max_fetched_at": delta_max_fetched_at,
                },
            )
            _record_step(current_step, step_t0)

            current_step = "swap_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                {"state_key": INFO_READ_MODEL_STATE_KEY, "version_id": version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "count_rows"
            step_t0 = time.time()
            card_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_card_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            scope_item_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_scope_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            if current_step != "read_active_version":
                error_message = f"{current_step}: {exc}"
                try:
                    conn.execute(
                        f"""UPDATE {schema}.info_read_model_versions
                               SET status = 'error',
                                   error_message = %(error_message)s,
                                   completed_at = now()
                             WHERE version_id = %(version_id)s::uuid""",
                        {"version_id": version_id, "error_message": error_message[:500]},
                    )
                    conn.commit()
                except Exception:
                    _rollback_safely(conn)
            raise RemoteDBError(f"info read model incremental refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "incremental",
        "version_id": version_id,
        "parent_version_id": active_version_id,
        "delta_items": delta_count,
        "card_items": int((card_row or {}).get("n") or 0),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_info_read_model(*, sample_limit: int = 200, min_github_stars: int = INFO_READ_MODEL_MIN_GITHUB_STARS) -> dict[str, Any]:
    """Build a versioned server read model for the 信息 tab platform view.

    The read model is intentionally version-swapped: readers keep using the
    previous complete version while this function builds a new one.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_sample_limit = max(50, min(int(sample_limit or 200), 1000))
    safe_min_github_stars = int(min_github_stars)
    where, params = _base_item_where(
        public_only=True,
        manual_owner_user_id=None,
        min_github_stars=safe_min_github_stars,
    )
    where.append("i.visible = 1")
    # perf-v27 P4: 热窗口 7 天——读模型只物化近 7 天内容(冷数据走 live/搜索)。
    # eligible 集从全量 visible(~70k)缩到一周量级,是本次物化瘦身的分母杠杆。
    where.append("i.fetched_at > now() - (%(info_window_days)s::int * interval '1 day')")
    params["info_window_days"] = INFO_READ_MODEL_WINDOW_DAYS
    _add_ai_relevance_filter(where)
    where_sql = _where_sql(where)
    section_category_expr = _section_category_expr("i")
    eligible_select_sql = f"""
                       SELECT i.id, i.user_id, i.platform, i.source, i.title,
                              i.author_name, i.author_id, i.author_avatar,
                              i.url, i.cover_url, i.media_json, i.metrics_json,
                              i.lang, i.description, i.detail_json,
                              i.ai_summary, i.ai_category, i.ai_keywords,
                              i.ai_categories, i.ai_subcategories,
                              i.content_type, i.visible, i.relevance_score,
                              i.fetched_at, i.published_at, i.created_at,
                              COALESCE(i.published_at, i.fetched_at) AS sort_at,
                              {section_category_expr} AS section_category
                         FROM {schema}.items i
                         {where_sql}
                     """
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    with connect() as conn:
        # BF-0706-4: 单飞锁 —— 已有重建在跑就跳过,避免并发叠加压崩 compute。
        got_lock = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (_INFO_READ_MODEL_BUILD_LOCK_KEY,)
        ).fetchone()["locked"]
        if not got_lock:
            return {"ok": True, "skipped": "build_in_progress"}
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "create_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_versions (
                       version_id, status, generated_at, sample_limit, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(sample_limit)s,
                       %(meta_json)s::jsonb
                     )""",
                {
                    "version_id": version_id,
                    "sample_limit": safe_sample_limit,
                    "meta_json": json.dumps({
                        "min_github_stars": safe_min_github_stars,
                        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
                        "scope_profile": INFO_READ_MODEL_SCOPE_PROFILE,
                    }),
                },
            )
            conn.commit()
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            _record_step(current_step, step_t0)
            current_step = "materialize_eligible"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_eligible")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_eligible ON COMMIT DROP AS
                    {eligible_select_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.info_read_model_eligible")
            _record_step(current_step, step_t0)
            current_step = "insert_card_items"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_card_items (
                       version_id, item_id, card_json, platform, source,
                       sort_at, fetched_at, published_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid,
                            i.id::text,
                            jsonb_strip_nulls(jsonb_build_object(
                              'id', i.id::text,
                              'user_id', i.user_id,
                              'platform', i.platform,
                              'source', i.source,
                              'title', i.title,
                              'author_name', i.author_name,
                              'author_id', i.author_id,
                              'author_avatar', i.author_avatar,
                              'url', i.url,
                              'cover_url', i.cover_url,
                              'media_json', i.media_json,
                              'metrics_json', i.metrics_json,
                              'lang', i.lang,
                              'description', i.description,
                              'ai_summary', left(i.ai_summary, 280),
                              'ai_category', i.ai_category,
                              'ai_categories', i.ai_categories,
                              'content_type', i.content_type,
                              'visible', i.visible,
                              'relevance_score', i.relevance_score,
                              'fetched_at', i.fetched_at,
                              'published_at', i.published_at,
                              'created_at', i.created_at,
                              'read_at', NULL,
                              'clicked_at', NULL,
                              'starred_at', NULL,
                              'hidden_at', NULL
                            )),
                            i.platform,
                            i.source,
                            i.sort_at,
                            i.fetched_at,
                            i.published_at,
                            i.relevance_score
                       FROM pg_temp.info_read_model_eligible i""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "materialize_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_scope_rows")
            # perf-v27 P4: 内联 UNION 副本改走共享 helper——ENG-0710 时内联副本
            # 差点漏砍的教训;单一事实源后砍维度只改 helper 一处。
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_scope_rows ON COMMIT DROP AS
                    {_info_read_model_scope_rows_select("pg_temp.info_read_model_eligible")}""",
                {
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                    "compound_separator": INFO_SCOPE_COMPOUND_SEPARATOR,
                },
            )
            conn.execute("ANALYZE pg_temp.info_read_model_scope_rows")
            _record_step(current_step, step_t0)
            current_step = "insert_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_scopes (
                       version_id, scope_key, platform, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            platform,
                            dimension,
                            value,
                            count(*)::integer,
                            max(rank_at),
                            now()
                       FROM pg_temp.info_read_model_scope_rows
                      GROUP BY scope_key, platform, dimension, value""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "insert_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH ranked AS (
                       SELECT scope_key, item_id, sort_at, fetched_at, relevance_score,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY rank_at DESC NULLS LAST,
                                         fetched_at DESC NULLS LAST,
                                         relevance_score DESC NULLS LAST,
                                         item_id DESC
                              ) AS rn
                         FROM pg_temp.info_read_model_scope_rows
                     )
                     INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(version_id)s::uuid, scope_key, rn::integer, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM ranked
                      WHERE rn <= %(scope_top_n)s""",
                {"version_id": version_id, "scope_top_n": INFO_READ_MODEL_SCOPE_TOP_N},
            )
            _record_step(current_step, step_t0)
            current_step = "complete_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_fetched_at = (
                             SELECT max(fetched_at)
                               FROM {schema}.info_card_items
                              WHERE version_id = %(version_id)s::uuid
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {"version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "swap_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""INSERT INTO {schema}.info_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                {"state_key": INFO_READ_MODEL_STATE_KEY, "version_id": version_id},
            )
            _record_step(current_step, step_t0)
            current_step = "count_rows"
            step_t0 = time.time()
            card_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_card_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            scope_item_row = conn.execute(
                f"SELECT count(*) AS n FROM {schema}.info_scope_items WHERE version_id = %(version_id)s::uuid",
                {"version_id": version_id},
            ).fetchone()
            _record_step(current_step, step_t0)
            current_step = "prune_old_versions"
            step_t0 = time.time()
            _prune_info_read_model_versions(conn, schema=schema)
            _record_step(current_step, step_t0)
            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            error_message = f"{current_step}: {exc}"
            try:
                conn.execute(
                    f"""UPDATE {schema}.info_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": error_message[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError(f"info read model refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "version_id": version_id,
        "card_items": int((card_row or {}).get("n") or 0),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "sample_limit": safe_sample_limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def _events_read_model_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV,
        EVENTS_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _context_search_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_ENV,
        CONTEXT_SEARCH_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _info_read_model_idle_tx_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_ENV,
        INFO_READ_MODEL_IDLE_TX_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _feed_events_timeout_ms(env: dict[str, str] | None = None) -> int:
    """statement_timeout for the /api/feed/events read path (BF-0708-3).

    Default 30s: above the observed 21.4s cold query, far below Cloudflare's
    ~100s cutoff. Clamped to the ceiling so a misconfigured env cannot bring
    back the 524 white-screen.
    """
    value = _env_int(
        env if env is not None else _runtime_env(),
        FEED_EVENTS_TIMEOUT_MS_ENV,
        _FEED_EVENTS_TIMEOUT_DEFAULT_MS,
        min_value=1000,
    )
    return min(value, _FEED_EVENTS_TIMEOUT_CEILING_MS)


def _set_events_read_model_timeouts(conn: Any) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=_events_read_model_statement_timeout_ms(),
        idle_tx_timeout_ms=_events_read_model_idle_tx_timeout_ms(),
    )


def _set_context_search_timeouts(
    conn: Any,
    *,
    statement_timeout_ms: int | None = None,
) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=statement_timeout_ms or _context_search_statement_timeout_ms(),
        idle_tx_timeout_ms=_context_search_idle_tx_timeout_ms(),
    )


def _set_info_read_model_timeouts(conn: Any, *, statement_timeout_ms: int = 2500) -> bool:
    return _set_local_statement_and_idle_tx_timeouts(
        conn,
        statement_timeout_ms=statement_timeout_ms,
        idle_tx_timeout_ms=_info_read_model_idle_tx_timeout_ms(),
    )


def _remote_feed_live_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_FEED_LIVE_TIMEOUT_MS_ENV, 2500, min_value=500)


def _remote_feed_search_timeout_ms() -> int:
    # 搜索比常规 feed 读昂贵(索引命中后仍需回表匹配行),独立预算
    return _env_int(
        _runtime_env(),
        REMOTE_FEED_SEARCH_TIMEOUT_MS_ENV,
        REMOTE_FEED_SEARCH_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _feed_more_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        FEED_MORE_TIMEOUT_MS_ENV,
        FEED_MORE_TIMEOUT_MS_DEFAULT,
        min_value=1000,
    )


def _remote_feed_live_runtime_circuit_open() -> bool:
    """仅运行时熔断窗(live 刚失败后的保护期),不含 env 级 LIVE_DISABLED。

    ENG-0710 降维瘦身:section_subcategory 视图不再物化、只能走 live,
    env 级关闭对它不适用,但熔断窗必须尊重——live 失败后别继续锤库。
    """
    with _REMOTE_FEED_LIVE_CIRCUIT_LOCK:
        return time.monotonic() < _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL


def _remote_feed_live_circuit_open() -> bool:
    env = _runtime_env()
    if _truthy(env.get(REMOTE_FEED_LIVE_DISABLED_ENV)):
        return True
    return _remote_feed_live_runtime_circuit_open()


def _mark_remote_feed_live_circuit_open() -> None:
    global _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL
    hold_sec = _env_int(_runtime_env(), REMOTE_FEED_LIVE_CIRCUIT_SEC_ENV, 60, min_value=1)
    with _REMOTE_FEED_LIVE_CIRCUIT_LOCK:
        _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL = max(
            _REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL,
            time.monotonic() + hold_sec,
        )


def _read_feed_snapshot(
    conn: Any,
    schema: str,
    snapshot_key: str,
    *,
    allow_expired: bool = False,
) -> Any | None:
    availability_key = ("feed_snapshots_available", schema)
    if _cache_get_with_ttl(availability_key, 300) is False:
        return None
    try:
        expires_filter = "" if allow_expired else "AND (expires_at IS NULL OR expires_at > now())"
        row = conn.execute(
            f"""SELECT payload_json
                  FROM {schema}.feed_snapshots
                 WHERE snapshot_key = %s
                   {expires_filter}
                 ORDER BY generated_at DESC
                 LIMIT 1""",
            (snapshot_key,),
        ).fetchone()
    except Exception:
        _rollback_safely(conn)
        _cache_set_with_ttl(availability_key, False, 300)
        return None
    _cache_set_with_ttl(availability_key, True, 300)
    if not row:
        return None
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if allow_expired:
        return _mark_stale_payload(payload, source="feed_snapshots")
    return payload


def _write_feed_snapshot(conn: Any, schema: str, snapshot_key: str, payload: Any) -> None:
    ttl = _remote_snapshot_ttl()
    if ttl <= 0 or not _feed_snapshots_available(conn, schema):
        return
    try:
        conn.execute(
            f"""INSERT INTO {schema}.feed_snapshots
                  (snapshot_key, payload_json, generated_at, expires_at)
                VALUES (%s, %s, now(), now() + (%s * interval '1 second'))
                ON CONFLICT (snapshot_key) DO UPDATE SET
                  payload_json = excluded.payload_json,
                  generated_at = excluded.generated_at,
                  expires_at = excluded.expires_at""",
            (snapshot_key, _maybe_jsonb(payload), int(ttl)),
        )
        conn.commit()
    except Exception:
        _rollback_safely(conn)


def _events_snapshot_key(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
    display_threshold: float | None = None,
) -> str:
    cats = ",".join(sorted(categories or []))
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    display = "off" if display_threshold is None else repr(float(display_threshold))
    return (
        "events:v5:"
        f"before={highlights_published_before().date().isoformat()}:"
        f"limit={int(limit)}:"
        f"public={int(bool(public_only))}:"
        f"stars={int(min_github_stars)}:"
        f"enabled={int(bool(enabled))}:"
        f"tz={tz_offset}:"
        f"display={display}:"
        f"cats={cats}"
    )


def _feed_events_local_cache_name(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
    display_threshold: float | None = None,
) -> str:
    cats = ",".join(sorted(categories or []))
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    display = "off" if display_threshold is None else repr(float(display_threshold))
    return (
        f"feed_events_limit={int(limit)}_"
        f"v5_before={highlights_published_before().date().isoformat()}_"
        f"public={int(bool(public_only))}_"
        f"stars={int(min_github_stars)}_"
        f"enabled={int(bool(enabled))}_"
        f"tz={tz_offset}_"
        f"display={display}_"
        f"cats={cats}"
    )


def _feed_items_local_cache_name(
    *,
    limit: int,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return (
        f"feed_items_limit={int(limit)}_"
        f"public={int(bool(public_only))}_"
        f"stars={int(min_github_stars)}"
    )


def _sections_snapshot_key(
    *,
    per_category: int | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> str:
    return (
        "sections:v1:"
        f"per={per_category if per_category is not None else 'all'}:"
        f"public={int(bool(public_only))}:"
        f"owner={manual_owner_user_id or ''}:"
        f"stars={int(min_github_stars)}"
    )


def _run_has_item_records_remote(conn: Any, run_id: int) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {remote_schema()}.fetch_run_items WHERE run_id = %s LIMIT 1",
        (run_id,),
    ).fetchone()
    return row is not None


def _new_run_items_sql_remote(conn: Any, run_id: int) -> tuple[str, dict[str, Any], str]:
    schema = remote_schema()
    if _run_has_item_records_remote(conn, run_id):
        return (
            f"""SELECT i.*
                  FROM {schema}.fetch_run_items fri
                  JOIN {schema}.items i ON i.id = fri.item_id
                 WHERE fri.run_id = %(run_id)s
                   AND fri.was_inserted = 1""",
            {"run_id": run_id},
            "fetch_run_items",
        )
    return (
        f"""SELECT i.*
              FROM {schema}.items i
              JOIN {schema}.fetch_runs r ON r.id = %(run_id)s
             WHERE i.fetch_run_id = r.id
               AND i.created_at >= r.started_at
               AND i.created_at <= coalesce(r.finished_at, now())""",
        {"run_id": run_id},
        "created_at_fallback",
    )


def _remote_pill_from_item(row: dict[str, Any]) -> str:
    cats = _json_value(row.get("ai_categories"))
    if isinstance(cats, list) and cats:
        return str(cats[0] or "_uncategorized")
    return canonicalize_category(row.get("ai_category")) or "_uncategorized"


def _extract_fetch_errors(stats: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(stats, dict):
        return errors
    for key, value in stats.items():
        if str(key).startswith("_"):
            continue
        if isinstance(value, dict):
            for err in value.get("errors") or []:
                errors.append({"scope": str(key), "message": str(err)})
        elif isinstance(value, list):
            for err in value:
                errors.append({"scope": str(key), "message": str(err)})
    return errors[:20]


def _has_duplicate_item_ids(batch: list[dict[str, Any]]) -> bool:
    seen: set[str] = set()
    for item in batch:
        item_id = item.get("id")
        if item_id is None:
            continue
        key = str(item_id)
        if key in seen:
            return True
        seen.add(key)
    return False


def _item_upsert_sql(schema: str, *, row_count: int = 1) -> str:
    columns = REMOTE_ITEM_WRITE_COLUMNS
    col_sql = ", ".join(columns)
    placeholders = _multirow_values_placeholder(len(columns), row_count)
    refresh_change_condition = _item_upsert_read_model_refresh_condition()
    noop_guard = _item_upsert_noop_guard(refresh_change_condition)
    return f"""INSERT INTO {schema}.items AS target ({col_sql})
            VALUES {placeholders}
            ON CONFLICT (id) DO UPDATE SET
              content = CASE
                WHEN length(COALESCE(excluded.content, '')) > length(COALESCE(target.content, ''))
                THEN excluded.content ELSE target.content END,
              url = COALESCE(NULLIF(excluded.url, ''), target.url),
              ai_summary = COALESCE(excluded.ai_summary, target.ai_summary),
              ai_key_points = COALESCE(excluded.ai_key_points, target.ai_key_points),
              metrics_json = excluded.metrics_json,
              detail_json = COALESCE(excluded.detail_json, target.detail_json),
              comments_json = excluded.comments_json,
              asr_text = COALESCE(excluded.asr_text, target.asr_text),
              asr_status = COALESCE(excluded.asr_status, target.asr_status),
              asr_duration_sec = COALESCE(excluded.asr_duration_sec, target.asr_duration_sec),
              asr_cost_yuan = COALESCE(excluded.asr_cost_yuan, target.asr_cost_yuan),
              asr_attempted_at = COALESCE(excluded.asr_attempted_at, target.asr_attempted_at),
              asr_failed_reason = COALESCE(excluded.asr_failed_reason, target.asr_failed_reason),
              asr_provider = COALESCE(excluded.asr_provider, target.asr_provider),
              asr_segments = COALESCE(excluded.asr_segments, target.asr_segments),
              asr_text_cn = COALESCE(excluded.asr_text_cn, target.asr_text_cn),
              asr_segments_cn = COALESCE(excluded.asr_segments_cn, target.asr_segments_cn),
              cover_url = COALESCE(excluded.cover_url, target.cover_url),
              media_json = COALESCE(excluded.media_json, target.media_json),
              author_name = COALESCE(NULLIF(excluded.author_name, ''), target.author_name),
              source = COALESCE(NULLIF(excluded.source, ''), target.source),
              source_id = COALESCE(excluded.source_id, target.source_id),
              fetch_run_id = COALESCE(excluded.fetch_run_id, target.fetch_run_id),
              fetched_at = CASE
                WHEN excluded.fetch_run_id IS NOT NULL
                     AND {refresh_change_condition}
                THEN excluded.fetched_at
                ELSE target.fetched_at END
            WHERE {noop_guard}"""


def _item_upsert_read_model_refresh_condition() -> str:
    """Return true when an existing item update changes info read-model content."""
    return """(
                COALESCE(NULLIF(excluded.url, ''), target.url) IS DISTINCT FROM target.url
                OR COALESCE(excluded.ai_summary, target.ai_summary) IS DISTINCT FROM target.ai_summary
                OR excluded.metrics_json IS DISTINCT FROM target.metrics_json
                OR COALESCE(excluded.detail_json, target.detail_json) IS DISTINCT FROM target.detail_json
                OR COALESCE(excluded.cover_url, target.cover_url) IS DISTINCT FROM target.cover_url
                OR COALESCE(excluded.media_json, target.media_json) IS DISTINCT FROM target.media_json
                OR COALESCE(NULLIF(excluded.author_name, ''), target.author_name) IS DISTINCT FROM target.author_name
                OR COALESCE(NULLIF(excluded.source, ''), target.source) IS DISTINCT FROM target.source
              )"""


def _item_upsert_noop_guard(refresh_change_condition: str) -> str:
    return f"""(
                CASE
                  WHEN length(COALESCE(excluded.content, '')) > length(COALESCE(target.content, ''))
                  THEN excluded.content ELSE target.content END IS DISTINCT FROM target.content
                OR COALESCE(NULLIF(excluded.url, ''), target.url) IS DISTINCT FROM target.url
                OR COALESCE(excluded.ai_summary, target.ai_summary) IS DISTINCT FROM target.ai_summary
                OR COALESCE(excluded.ai_key_points, target.ai_key_points) IS DISTINCT FROM target.ai_key_points
                OR excluded.metrics_json IS DISTINCT FROM target.metrics_json
                OR COALESCE(excluded.detail_json, target.detail_json) IS DISTINCT FROM target.detail_json
                OR excluded.comments_json IS DISTINCT FROM target.comments_json
                OR COALESCE(excluded.asr_text, target.asr_text) IS DISTINCT FROM target.asr_text
                OR COALESCE(excluded.asr_status, target.asr_status) IS DISTINCT FROM target.asr_status
                OR COALESCE(excluded.asr_duration_sec, target.asr_duration_sec) IS DISTINCT FROM target.asr_duration_sec
                OR COALESCE(excluded.asr_cost_yuan, target.asr_cost_yuan) IS DISTINCT FROM target.asr_cost_yuan
                OR COALESCE(excluded.asr_attempted_at, target.asr_attempted_at) IS DISTINCT FROM target.asr_attempted_at
                OR COALESCE(excluded.asr_failed_reason, target.asr_failed_reason) IS DISTINCT FROM target.asr_failed_reason
                OR COALESCE(excluded.asr_provider, target.asr_provider) IS DISTINCT FROM target.asr_provider
                OR COALESCE(excluded.asr_segments, target.asr_segments) IS DISTINCT FROM target.asr_segments
                OR COALESCE(excluded.asr_text_cn, target.asr_text_cn) IS DISTINCT FROM target.asr_text_cn
                OR COALESCE(excluded.asr_segments_cn, target.asr_segments_cn) IS DISTINCT FROM target.asr_segments_cn
                OR COALESCE(excluded.cover_url, target.cover_url) IS DISTINCT FROM target.cover_url
                OR COALESCE(excluded.media_json, target.media_json) IS DISTINCT FROM target.media_json
                OR COALESCE(NULLIF(excluded.author_name, ''), target.author_name) IS DISTINCT FROM target.author_name
                OR COALESCE(NULLIF(excluded.source, ''), target.source) IS DISTINCT FROM target.source
                OR COALESCE(excluded.source_id, target.source_id) IS DISTINCT FROM target.source_id
                OR (
                  excluded.fetch_run_id IS NOT NULL
                  AND {refresh_change_condition}
                  AND excluded.fetched_at IS DISTINCT FROM target.fetched_at
                )
              )"""


def add_feedback_remote(item_id: str, fb_type: str, topic: str | None = None, text: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.feedback (item_id, type, topic, text)
                VALUES (%s, %s, %s, %s)""",
            (item_id, fb_type, topic, text),
        )
        conn.commit()


def record_item_feedback_remote(
    *,
    item_id: str,
    action: str,
    platform: str | None = None,
    title: str | None = None,
    author: str | None = None,
    url: str | None = None,
    reason: str | None = None,
    topic: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.item_feedback
                  (item_id, platform, item_title, item_author, item_url,
                   action, reason, topic_at_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (item_id, platform, title, author, url, action, reason, topic),
        )
        conn.commit()


def get_feedback_item_context_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, title, author_name, url, ai_summary
                  FROM {remote_schema()}.items
                 WHERE id = %s""",
            (item_id,),
        ).fetchone()
    return dict(row) if row else None


# ── v21.0 action-revival: 行动点生成每日配额(remote 镜像 db.* 同名函数)──

def _generation_usage_snapshot(day_cst: str, used: int, limit: int) -> dict[str, Any]:
    from datetime import datetime, timedelta
    try:
        next_day = (datetime.strptime(day_cst, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    used = max(0, int(used))
    return {
        'day_cst': day_cst,
        'used': used,
        'limit': int(limit),
        'remaining': max(0, int(limit) - used),
        'over_limit': used >= int(limit),
        'reset_at': reset_at,
    }


def _feed_cols(
    status_alias: str | None = None,
    *,
    include_content: bool = False,
    include_heavy_json: bool = True,
) -> str:
    status_cols = (
        f"""{status_alias}.read_at, {status_alias}.clicked_at,
            {status_alias}.starred_at, {status_alias}.hidden_at"""
        if status_alias
        else """NULL::timestamptz AS read_at, NULL::timestamptz AS clicked_at,
                NULL::timestamptz AS starred_at, NULL::timestamptz AS hidden_at"""
    )
    content_col = "i.content," if include_content else ""
    # List/card endpoints do not render these detail-style JSON/text blobs.
    # Returning NULL aliases keeps the row shape stable while avoiding TOASTing
    # large fields such as ai_key_points for every card.
    tags_col = "i.tags_json" if include_heavy_json else "NULL::jsonb AS tags_json"
    heavy_json_cols = (
        "i.detail_json, i.comments_json,"
        if include_heavy_json
        else "NULL::jsonb AS detail_json, NULL::jsonb AS comments_json,"
    )
    ai_key_points_col = "i.ai_key_points" if include_heavy_json else "NULL::text AS ai_key_points"
    ai_keywords_col = "i.ai_keywords" if include_heavy_json else "NULL::text AS ai_keywords"
    ai_subcategories_col = "i.ai_subcategories" if include_heavy_json else "NULL::jsonb AS ai_subcategories"
    multi_l1_reason_col = "i.multi_l1_reason" if include_heavy_json else "NULL::text AS multi_l1_reason"
    ai_extracted_col = "i.ai_extracted" if include_heavy_json else "NULL::jsonb AS ai_extracted"
    asr_cols = (
        """i.asr_text, i.asr_status, i.asr_duration_sec, i.asr_cost_yuan,
           i.asr_attempted_at, i.asr_failed_reason, i.asr_provider,
           i.asr_segments, i.asr_text_cn, i.asr_segments_cn,"""
        if include_content
        else ""
    )
    return f"""
      i.id, i.user_id, i.platform, i.source, i.title, i.author_name, i.author_id,
      {content_col}
      i.author_avatar, i.url, i.cover_url, i.media_json, i.metrics_json,
      {tags_col}, i.lang, {heavy_json_cols} i.description,
      {asr_cols}
      i.ai_summary, {ai_key_points_col}, i.ai_category, {ai_keywords_col},
      i.ai_categories, {ai_subcategories_col}, {multi_l1_reason_col}, {ai_extracted_col},
      i.content_type, i.visible, i.relevance_score, i.fetched_at, i.published_at,
      i.created_at, {status_cols}
    """


def _normalize_item(raw: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    item = dict(raw)
    item.pop("embedding", None)
    item.pop("rn", None)
    item.pop("section_category", None)
    category = canonicalize_category(item.get("ai_category"))
    if category != item.get("ai_category"):
        item["ai_category"] = category
    for col in (
        "media_json",
        "metrics_json",
        "tags_json",
        "detail_json",
        "comments_json",
        "ai_categories",
        "ai_subcategories",
        "ai_extracted",
        "ai_key_points",
        "asr_segments",
        "asr_segments_cn",
    ):
        if col in item:
            item[col] = _json_value(item.get(col))
    for col in (
        "fetched_at",
        "published_at",
        "created_at",
        "read_at",
        "clicked_at",
        "starred_at",
        "hidden_at",
        "asr_attempted_at",
    ):
        if col in item:
            item[col] = _timestamp_value(item.get(col))
    if not detail:
        item.pop("detail_json", None)
        item.pop("comments_json", None)
        for col in (
            "tags_json",
            "ai_key_points",
            "ai_keywords",
            "ai_subcategories",
            "multi_l1_reason",
            "ai_extracted",
        ):
            item.pop(col, None)
    return item


def _manual_item_filter(
    alias: str,
    *,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if public_only:
        where.append(f"{alias}.platform != 'manual'")
        where.append(f"{alias}.user_id IS NULL")
    elif manual_owner_user_id:
        where.append(
            f"(({alias}.platform != 'manual' AND {alias}.user_id IS NULL) "
            f"OR {alias}.user_id = %(manual_owner_user_id)s)"
        )
        params["manual_owner_user_id"] = manual_owner_user_id
    return where, params


def _item_display_filter(alias: str, min_github_stars: int = 50) -> list[str]:
    where = [
        f"({alias}.source IS NULL OR {alias}.source NOT LIKE 'search:%%')",
        _info_display_source_filter(alias),
    ]
    if min_github_stars > 0:
        where.append(
            f"""({alias}.platform != 'github' OR (
              {alias}.metrics_json IS NOT NULL
              AND ({alias}.metrics_json ->> 'stars') ~ '^[0-9]+$'
              AND ({alias}.metrics_json ->> 'stars')::integer >= {int(min_github_stars)}
            ))"""
        )
    return where


def _base_item_where(
    *,
    alias: str = "i",
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> tuple[list[str], dict[str, Any]]:
    where, params = _manual_item_filter(
        alias,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
    )
    where.extend(_item_display_filter(alias, min_github_stars=min_github_stars))
    return where, params


def _add_ai_relevance_filter(where: list[str], *, alias: str = "i") -> None:
    """v18.0 nav-merge: 强制 AI 相关性过滤（信息 tab 复用 query_feed_platforms）。

    PRD §Spec-2 锁定口径（D3）：
        (ai_category IS NOT NULL AND ai_category != 'other')
     OR (ai_categories IS NOT NULL AND ai_categories::text NOT IN ('[]','null','"null"'))

    Postgres 注：ai_categories 是 jsonb，与字面量字符串比较需 ::text cast。
    """
    where.append(
        f"((({alias}.ai_category IS NOT NULL AND {alias}.ai_category != 'other')"
        f" OR ({alias}.ai_categories IS NOT NULL"
        f" AND {alias}.ai_categories::text NOT IN ('[]', 'null', '\"null\"'))))"
    )


def _where_sql(where: list[str]) -> str:
    return "WHERE " + " AND ".join(where) if where else ""


def _add_search_filter(
    where: list[str],
    params: dict[str, Any],
    search: str | None,
    *,
    param_key: str = "search_like",
):
    if not search:
        return
    params[param_key] = f"%{search}%"
    where.append(
        "(coalesce(i.title, '') || ' ' || coalesce(i.author_name, '') || ' ' || "
        f"coalesce(i.ai_summary, '') || ' ' || coalesce(i.ai_keywords::text, '')) ILIKE %({param_key})s"
    )


def _add_category_filter(where: list[str], params: dict[str, Any], category: str | None):
    if not category:
        return
    if category == UNCATEGORIZED_SENTINEL:
        where.append("i.ai_categories IS NULL")
        return
    params["category"] = category
    where.append(
        """EXISTS (
          SELECT 1 FROM jsonb_array_elements_text(i.ai_categories) AS cat(value)
          WHERE cat.value = %(category)s
        )"""
    )


def _fetch_items(conn: Any, schema: str, where: list[str], params: dict[str, Any],
                 *, order_sql: str, limit: int | None = None, offset: int = 0,
                 detail: bool = False, status_user_id: str | None = None) -> list[dict[str, Any]]:
    qparams = dict(params)
    status_join, status_params, status_alias = _item_status_join(schema, status_user_id)
    qparams.update(status_params)
    limit_sql = ""
    if limit is not None and limit > 0:
        qparams["limit"] = limit
        qparams["offset"] = max(0, offset)
        limit_sql = "LIMIT %(limit)s OFFSET %(offset)s"
    elif offset > 0:
        qparams["offset"] = offset
        limit_sql = "OFFSET %(offset)s"
    rows = conn.execute(
        f"""SELECT {_feed_cols(status_alias, include_content=detail, include_heavy_json=detail)}
              FROM {schema}.items i
              {status_join}
              {_where_sql(where)}
              {order_sql}
              {limit_sql}""",
        qparams,
    ).fetchall()
    return [_normalize_item(dict(r), detail=detail) for r in rows]


def _category_l1(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if "[" in raw:
        raw = raw.split("[", 1)[0]
    category = canonicalize_category(raw)
    if not category or category == "other" or category not in ACTIVE_CATEGORY_IDS:
        return None
    return category


def _row_to_event(
    row: dict[str, Any],
    *,
    user_last_seen: dict[int, int | None] | None = None,
    source_metadata: dict[int, dict[str, Any]] | None = None,
) -> dict:
    seen_map = user_last_seen or {}
    cid = int(row["id"])
    live_version = int(row.get("live_version") or 0)
    seen = seen_map.get(cid)
    metadata = (source_metadata or {}).get(cid, {})
    event = {
        "id": cid,
        "ai_title": row.get("ai_title"),
        "ai_summary": row.get("ai_summary"),
        "why_read": row.get("why_read"),
        "doc_count": int(row.get("doc_count") or 0),
        "unique_source_count": int(row.get("unique_source_count") or 0),
        "category": metadata.get("category"),
        "source_preview": metadata.get("source_preview", []),
        "first_doc_at": to_utc_iso(row.get("first_doc_at")) or row.get("first_doc_at"),
        "last_doc_at": to_utc_iso(row.get("last_doc_at")) if row.get("last_doc_at") else None,
        "platforms": _json_array(row.get("platforms_json")),
        "cover_url": row.get("cover_url"),
        "media_kind": metadata.get("media_kind") or ("image" if row.get("cover_url") else None),
        "has_update": bool(seen is not None and live_version > seen),
        "live_version": live_version,
        "last_seen_version": seen,
    }
    if "max_flag_score10" in row:
        event["display_score"] = _display_score_from_max_flag_score10(
            row.get("max_flag_score10")
        )
    return event


def fetch_events(
    *,
    page: int = 1,
    limit: int = 20,
    cursor: dict[str, Any] | None = None,
    since_version_snapshot: int | None = None,
    fetched_since: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    min_github_stars: int = 50,
    enabled: bool = False,
    categories: list[str] | None = None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
    target_date: str | None = None,
) -> dict:
    """P0-2(C 端放量):内容与用户状态分离。

    内容(clusters timeline、total、date_counts)与用户无关,由
    ``_fetch_events_content`` 计算并按「匿名/登录」两个 scope 共享缓存——
    N 个登录用户共用一份内容缓存和 singleflight,Supabase 压力与用户数
    解耦。逐用户的 seen 状态(has_update/last_seen_version)在返回前用
    一条索引查询薄覆盖;覆盖失败只降级状态,不影响内容。
    """
    result = _fetch_events_content(
        page=page,
        limit=limit,
        cursor=cursor,
        since_version_snapshot=since_version_snapshot,
        fetched_since=fetched_since,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
        enabled=enabled,
        categories=categories,
        timezone_offset_minutes=timezone_offset_minutes,
        target_date=target_date,
    )
    if user_id:
        _overlay_user_cluster_seen(result, user_id)
    return result


def _fetch_events_content(
    *,
    page: int = 1,
    limit: int = 20,
    cursor: dict[str, Any] | None = None,
    since_version_snapshot: int | None = None,
    fetched_since: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    min_github_stars: int = 50,
    enabled: bool = False,
    categories: list[str] | None = None,
    timezone_offset_minutes: int = _DEFAULT_TIMELINE_TIMEZONE_OFFSET_MINUTES,
    target_date: str | None = None,
) -> dict:
    """Fetch visible events from Supabase/Postgres in the API response shape.

    输出必须与用户无关(seen 字段为匿名默认值,由 fetch_events 覆盖);
    ``user_id`` 仅参与 read model 资格与快照条件判断,缓存 key 只取
    ``bool(user_id)`` 区分「匿名公共 / 登录」两种内容口径(public_only
    过滤不同)。

    v17.0: categories OR 多选 — 精选 tab L1 chip 筛选。空列表 / None = 不筛选。
    """
    schema = remote_schema()
    tz_offset = _timezone_offset_minutes(timezone_offset_minutes)
    published_before = highlights_published_before()
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    display_threshold = _highlights_display_threshold()
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=display_threshold,
    )
    # v17.0: categories filter（Postgres split_part 提取 L1 段）
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    where_sql = f"""
      c.is_visible_in_feed = true
      AND c.published_at IS NOT NULL
      AND coalesce(c.archived, false) = false
      AND c.merged_into IS NULL
      AND c.last_updated_at > now() - interval '30 days'
      AND COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) < %(published_before)s
      AND (
        %(fetched_since)s::timestamptz IS NULL
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci
          JOIN {schema}.items i ON i.id = ci.item_id
          WHERE ci.cluster_id = c.id
            AND i.fetched_at >= %(fetched_since)s::timestamptz
        )
      )
      {public_filter}
      {github_filter}
      {verdict_filter}
      {display_filter}
      {categories_filter}
    """
    params = {
        "fetched_since": fetched_since,
        "limit_plus_one": limit + 1,
        "offset": offset,
        "snapshot": since_version_snapshot,
        "categories": categories or [],
        "timezone_offset_minutes": tz_offset,
        "published_before": published_before,
    }
    cursor_cache_key = json.dumps(cursor, sort_keys=True, default=str) if cursor else ""
    result_cache_key = (
        "events_result_30d_v5",  # v5: 内容缓存去 user 化(P0-2),只按登录与否分桶
        published_before,
        schema,
        int(page),
        int(limit),
        cursor_cache_key,
        since_version_snapshot,
        fetched_since or "",
        bool(user_id),
        bool(public_only),
        int(min_github_stars),
        bool(enabled),
        tz_offset,
        display_threshold,
        tuple(categories or []),
    )
    prefer_highlights_read_model = (
        _highlights_read_model_enabled()
        and fetched_since is None
        and since_version_snapshot is None
        and (public_only or bool(user_id))
        and int(min_github_stars) == HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS
        and _highlights_scope_for_categories(categories) is not None
    )
    highlights_stale_freshness: dict[str, Any] | None = None
    highlights_self_heal: dict[str, Any] | None = None
    skip_stale_snapshot_fallback = False
    if (
        prefer_highlights_read_model
        and page == 1
        and cursor is None
        and _highlights_stale_fallback_enabled()
        and _highlights_request_freshness_enabled()
    ):
        try:
            freshness = highlights_read_model_freshness(min_github_stars=min_github_stars)
            if freshness.get("stale"):
                highlights_stale_freshness = freshness
                highlights_self_heal = _trigger_highlights_read_model_self_heal(
                    reason=str(freshness.get("reason") or "stale"),
                    min_interval_sec=60,
                )
                prefer_highlights_read_model = False
                skip_stale_snapshot_fallback = True
        except Exception as exc:
            highlights_stale_freshness = {
                "ok": False,
                "stale": None,
                "error": str(exc)[:200],
            }
    if not skip_stale_snapshot_fallback and target_date is None:
        cached_result = _cache_get_copy(result_cache_key)
        if cached_result is not None:
            return cached_result
    snapshot_key = None
    local_cache_name = None
    expired_snapshot_fallback = None
    if (
        page == 1
        and limit == 20
        and since_version_snapshot is None
        and fetched_since is None
        and not user_id
        and target_date is None
    ):
        snapshot_key = _events_snapshot_key(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
            enabled=enabled,
            categories=categories,
            timezone_offset_minutes=tz_offset,
            display_threshold=display_threshold,
        )
        local_cache_name = _feed_events_local_cache_name(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
            enabled=enabled,
            categories=categories,
            timezone_offset_minutes=tz_offset,
            display_threshold=display_threshold,
        )
        fresh_fallback = _read_local_read_cache(
            local_cache_name,
            max_age_sec=_LOCAL_READ_CACHE_FRESH_SEC,
        )
        if fresh_fallback is not None and not prefer_highlights_read_model:
            return _cache_set_copy(result_cache_key, fresh_fallback)
    if prefer_highlights_read_model:
        def _compute_highlights_result() -> dict[str, Any] | None:
            cached_inside = _cache_get_copy(result_cache_key) if target_date is None else None
            if cached_inside is not None:
                return cached_inside
            with connect() as conn:
                # BF-0708-3: bound this read. Without it the query ran 129s on a
                # cold cache and Cloudflare cut the connection at ~100s (524),
                # leaving the feed stuck on skeletons forever. On timeout the
                # route serves the last-good snapshot instead.
                _set_short_statement_timeout(conn, _feed_events_timeout_ms())
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                    display_threshold=display_threshold,
                    **({"target_date": target_date} if target_date else {}),
                )
            if read_model_result is not None:
                if snapshot_key:
                    _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                    if local_cache_name:
                        _write_local_read_cache_async(local_cache_name, read_model_result)
                return read_model_result if target_date else _cache_set_copy(result_cache_key, read_model_result)
            return None

        try:
            read_model_result = _compute_highlights_result() if target_date else _singleflight_sync(
                ("events_highlights_read_model", *result_cache_key),
                _compute_highlights_result,
            )
        except RemoteDBError:
            if target_date:
                raise
            if page == 1 and not user_id and since_version_snapshot is None and fetched_since is None:
                if local_cache_name:
                    stale_fallback = _read_local_read_cache(local_cache_name)
                    if stale_fallback is not None:
                        return _cache_set_copy(
                            result_cache_key,
                            _mark_stale_payload(stale_fallback, source="local_read_cache"),
                        )
                return {
                    "enabled": enabled,
                    "events": [],
                    "next_cursor": None,
                    "new_since_last_fetch": 0,
                    "total_available_within_30d": 0,
                    "date_counts": {},
                    "data_backend": event_read_backend(),
                    "degraded": True,
                }
            raise
        if read_model_result is not None:
            return read_model_result
        prefer_highlights_read_model = False
    total_cache_key = (
        "events_total_30d",
        published_before,
        schema,
        bool(public_only),
        bool(user_id),
        int(min_github_stars),
        fetched_since or "",
        display_threshold,
        tuple(categories or []),
    )
    date_counts_cache_key = (
        "events_date_counts_30d",
        published_before,
        schema,
        bool(public_only),
        bool(user_id),
        int(min_github_stars),
        fetched_since or "",
        tz_offset,
        display_threshold,
        tuple(categories or []),
    )
    try:
        with connect() as conn:
            # BF-0708-3: bound EVERY feed read, not just the read-model branch.
            # When the read model goes stale, prefer_highlights_read_model flips
            # to False and we fall through to the live-aggregation path — which
            # is exactly the 129s query that made Cloudflare 524. Setting the
            # timeout only inside the read-model branch would leave the slow
            # path unbounded, which is the failure this bug is about.
            _set_short_statement_timeout(conn, _feed_events_timeout_ms())
            if prefer_highlights_read_model:
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                    display_threshold=display_threshold,
                    **({"target_date": target_date} if target_date else {}),
                )
                if read_model_result is not None:
                    if snapshot_key:
                        _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                        if local_cache_name:
                            _write_local_read_cache_async(local_cache_name, read_model_result)
                    return read_model_result if target_date else _cache_set_copy(result_cache_key, read_model_result)
            if snapshot_key and not skip_stale_snapshot_fallback:
                snapshot = _read_feed_snapshot(conn, schema, snapshot_key)
                if snapshot is not None:
                    return _cache_set_copy(result_cache_key, snapshot)
                expired_snapshot_fallback = _read_feed_snapshot(
                    conn,
                    schema,
                    snapshot_key,
                    allow_expired=True,
                )
            if not prefer_highlights_read_model:
                read_model_result = _query_highlights_read_model_events(
                    conn=conn,
                    schema=schema,
                    page=page,
                    limit=limit,
                    cursor=cursor,
                    since_version_snapshot=since_version_snapshot,
                    fetched_since=fetched_since,
                    user_id=user_id,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                    enabled=enabled,
                    categories=categories,
                    timezone_offset_minutes=tz_offset,
                    display_threshold=display_threshold,
                    **({"target_date": target_date} if target_date else {}),
                )
                if read_model_result is not None:
                    if snapshot_key:
                        _write_feed_snapshot_async(schema, snapshot_key, read_model_result)
                        if local_cache_name:
                            _write_local_read_cache_async(local_cache_name, read_model_result)
                    return read_model_result if target_date else _cache_set_copy(result_cache_key, read_model_result)
            seek_cte = ""
            seek_columns = ""
            seek_join = ""
            page_offset_sql = "%(offset)s"
            if target_date:
                seek_cte = _events_date_seek_cte(f"""
                    SELECT c.id AS cluster_id,
                           COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at,
                           row_number() OVER (ORDER BY c.first_doc_at DESC NULLS LAST,
                                                      c.last_updated_at DESC NULLS LAST, c.id DESC) - 1 AS position
                      FROM {schema}.clusters c
                     WHERE {where_sql}
                """)
                seek_columns = ", date_seek_anchor.anchor_event_id, date_seek_anchor.page_offset"
                seek_join = "CROSS JOIN date_seek_anchor"
                page_offset_sql = "COALESCE((SELECT page_offset FROM date_seek_anchor), 0)"
                params.update(_events_date_seek_params(target_date, tz_offset, limit))
            rows = conn.execute(
                f"""{seek_cte}SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.doc_count,
                           c.unique_source_count, c.first_doc_at, c.last_doc_at,
                           c.platforms_json,
                           COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                           c.live_version,
                           c.last_updated_at,
                           (d.score_inputs->>'max_flag_score10')::float AS max_flag_score10 {seek_columns}
                      FROM {schema}.clusters c
                      {seek_join}
                      LEFT JOIN LATERAL (
                        SELECT i.cover_url
                          FROM {schema}.cluster_items ci
                          JOIN {schema}.items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND NULLIF(i.cover_url, '') IS NOT NULL
                           AND i.platform <> 'manual'
                           AND i.user_id IS NULL
                         ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                                  ci.rank_in_cluster ASC NULLS LAST
                         LIMIT 1
                      ) event_cover ON true
                      LEFT JOIN {schema}.highlight_cluster_decisions d
                        ON d.cluster_id = c.id
                     WHERE {where_sql}
                     ORDER BY c.first_doc_at DESC NULLS LAST,
                              c.last_updated_at DESC NULLS LAST,
                              c.id DESC
                     LIMIT %(limit_plus_one)s OFFSET {page_offset_sql}""",
                params,
            ).fetchall()
            total_count = _cache_get(total_cache_key)
            if total_count is None:
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM {schema}.clusters c WHERE {where_sql}",
                    params,
                ).fetchone()
                total_count = int(total_row["n"] if total_row else 0)
                _cache_set(total_cache_key, total_count)
            date_counts = _cache_get_copy(date_counts_cache_key)
            if date_counts is None:
                date_count_rows = conn.execute(
                    f"""SELECT COALESCE(
                                  to_char((
                                    COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)
                                    - (%(timezone_offset_minutes)s::int * interval '1 minute')
                                  )::date, 'YYYY-MM-DD'),
                                  'unknown'
                                ) AS day,
                               count(*) AS n
                          FROM {schema}.clusters c
                         WHERE {where_sql}
                         GROUP BY day""",
                    params,
                ).fetchall()
                date_counts = {
                    str(row["day"] or "unknown"): int(row["n"] or 0)
                    for row in date_count_rows
                }
                _cache_set_copy(date_counts_cache_key, date_counts)
            new_since = 0
            if since_version_snapshot is not None:
                row = conn.execute(
                    f"""SELECT count(*) AS n
                          FROM {schema}.clusters c
                         WHERE {where_sql}
                           AND c.id > %(snapshot)s""",
                    params,
                ).fetchone()
                new_since = int(row["n"] if row else 0)
            # P0-2: seen 状态不在内容层查询,由 fetch_events 的 overlay 覆盖。
            seen_map: dict[int, int | None] = {}
            has_more = len(rows) > limit
            if target_date and rows:
                page = int(rows[0]["page_offset"]) // limit + 1
            page_rows = rows[:limit]
            source_metadata = _fetch_event_source_metadata(conn, schema, [int(r["id"]) for r in page_rows])
            result = {
                "enabled": enabled,
                "events": [
                    _row_to_event(dict(r), user_last_seen=seen_map, source_metadata=source_metadata)
                    for r in page_rows
                ],
                "next_cursor": (page + 1) if has_more else None,
                "new_since_last_fetch": new_since,
                "total_available_within_30d": int(total_count),
                "date_counts": date_counts,
                "data_backend": event_read_backend(),
            }
            if target_date:
                result["date_seek"] = _events_date_seek_result(target_date, rows)
            if highlights_stale_freshness is not None:
                result["read_model_stale"] = True
                result["fallback_reason"] = "highlights_read_model_stale"
                result["read_model_freshness"] = highlights_stale_freshness
                if highlights_self_heal is not None:
                    result["read_model_self_heal"] = highlights_self_heal
            if snapshot_key:
                _write_feed_snapshot_async(schema, snapshot_key, result)
                if local_cache_name:
                    _write_local_read_cache_async(local_cache_name, result)
        if target_date is None and _feed_result_cacheable(result):
            return _cache_set_copy(result_cache_key, result)
        return result
    except RemoteDBError:
        if target_date:
            raise
        if page == 1 and not user_id and since_version_snapshot is None and fetched_since is None:
            if expired_snapshot_fallback is not None:
                return _cache_set_copy(result_cache_key, expired_snapshot_fallback)
            if local_cache_name:
                stale_fallback = _read_local_read_cache(local_cache_name)
                if stale_fallback is not None:
                    return _cache_set_copy(
                        result_cache_key,
                        _mark_stale_payload(stale_fallback, source="local_read_cache"),
                    )
            result = {
                "enabled": enabled,
                "events": [],
                "next_cursor": None,
                "new_since_last_fetch": 0,
                "total_available_within_30d": 0,
                "date_counts": {},
                "data_backend": event_read_backend(),
                "degraded": True,
            }
            return result
        raise


def search_recommend_remote(
    *,
    q: str,
    limit: int = 30,
    public_only: bool = False,
    min_github_stars: int = 50,
    categories: list[str] | None = None,
) -> dict:
    """v17.0: Supabase 路径搜索 — recommend context 返回 docs + events 双区。

    docs: items 表 ILIKE (title/content/ai_summary/author_name/ai_keywords)
    events: clusters 表 ILIKE (ai_title/ai_summary), 可选 categories OR 筛选
            与 /api/feed/events 保持一致的 visibility 门槛 (is_visible_in_feed + unique_source_count>=2)
    """
    schema = remote_schema()
    pattern = f"%{q}%"
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    params: dict[str, Any] = {
        "pattern": pattern,
        "limit": limit,
        "categories": categories or [],
    }
    with connect() as conn:
        # docs 区 (items 表) — recommend context 与 channel 等共享 doc 维度搜索
        # 性能优化 (v17.0): 仅搜 title + ai_summary 短字段。content 是长文本字段,
        # ILIKE 全表扫描 63k 行约耗时 47s; 去掉后约 1-2s。如需 content 全文搜索,
        # 后续应建 tsvector + GIN 索引专项支持。
        doc_rows = conn.execute(
            f"""SELECT id, platform, title, author_name, published_at, ai_summary
                  FROM {schema}.items
                 WHERE title ILIKE %(pattern)s
                    OR ai_summary ILIKE %(pattern)s
                 ORDER BY coalesce(published_at, fetched_at) DESC
                 LIMIT %(limit)s""",
            params,
        ).fetchall()
        docs_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.items
                 WHERE title ILIKE %(pattern)s
                    OR ai_summary ILIKE %(pattern)s""",
            params,
        ).fetchone()
        # events 区 (clusters 表) — categories 叠加可选
        ev_where = f"""
          c.is_visible_in_feed = true
          AND coalesce(c.unique_source_count, 0) >= 2
          AND c.published_at IS NOT NULL
          AND coalesce(c.archived, false) = false
          AND c.merged_into IS NULL
          AND (c.ai_title ILIKE %(pattern)s OR c.ai_summary ILIKE %(pattern)s)
          {public_filter}
          {github_filter}
          {categories_filter}
        """
        ev_rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.doc_count,
                       c.unique_source_count, c.first_doc_at, c.last_doc_at,
                       c.platforms_json, c.cover_url, c.live_version
                  FROM {schema}.clusters c
                 WHERE {ev_where}
                 ORDER BY c.first_doc_at DESC
                 LIMIT %(limit)s""",
            params,
        ).fetchall()
        ev_total_row = conn.execute(
            f"SELECT count(*) AS n FROM {schema}.clusters c WHERE {ev_where}",
            params,
        ).fetchone()
        source_metadata = _fetch_event_source_metadata(conn, schema, [int(r["id"]) for r in ev_rows])
    return {
        "docs": [dict(r) for r in doc_rows],
        "docs_total": int(docs_total_row["n"]) if docs_total_row else 0,
        # v17.0 fix: events 区必须走 _row_to_event transformer
        # 否则前端收到 platforms_json (JSON 字符串) 而非 platforms (数组),
        # 来源图标渲染 fallback 灰色 "s"; unique_source_count 也缺失,
        # EventCard 来源徽章 (BF-0428-1) 失效
        "events": [_row_to_event(dict(r), source_metadata=source_metadata) for r in ev_rows],
        "events_total": int(ev_total_row["n"]) if ev_total_row else 0,
        "data_backend": event_read_backend(),
    }


def _library_item_entry(item: dict[str, Any], *, status_field: str) -> dict[str, Any]:
    return {
        "id": f"item:{item.get('id')}",
        "type": "item",
        "occurred_at": item.get(status_field) or item.get("fetched_at"),
        "item": item,
    }


def query_library(
    *,
    view: str,
    limit: int = 100,
    offset: int = 0,
    user_id: str,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict[str, Any]:
    if view not in ("history", "starred"):
        raise ValueError("view must be history or starred")
    schema = remote_schema()
    status_field = "clicked_at" if view == "history" else "starred_at"
    fetch_limit = max(1, int(limit) + int(offset))

    item_where, item_params = _base_item_where(
        public_only=False,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    item_where.append(f"s.{status_field} IS NOT NULL")
    item_order = f"ORDER BY s.{status_field} DESC NULLS LAST"

    cluster_params = {
        "user_id": user_id,
        "limit": fetch_limit,
    }
    cluster_status_predicate = f"s.{status_field} IS NOT NULL"
    cluster_privacy_filter = """
      AND NOT EXISTS (
        SELECT 1
          FROM {schema}.cluster_items ci_priv
          JOIN {schema}.items i_priv ON i_priv.id = ci_priv.item_id
         WHERE ci_priv.cluster_id = c.id
           AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
           AND COALESCE(i_priv.user_id, '') <> %(user_id)s
      )
    """.format(schema=schema)

    with connect() as conn:
        # BE-6: 收藏/历史是登录核心路径;cluster 页带两个 LATERAL 子查询,
        # 数据倾斜时可拖到 pooler 全局 120s 并占死池连接。与 feed live 同口径止损。
        _set_short_statement_timeout(conn, 4500)
        item_count_params = dict(item_params)
        item_count_join, item_status_params, _ = _item_status_join(schema, user_id)
        item_count_params.update(item_status_params)
        item_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.items i
                  {item_count_join}
                  {_where_sql(item_where)}""",
            item_count_params,
        ).fetchone()
        item_rows = _fetch_items(
            conn,
            schema,
            item_where,
            item_params,
            order_sql=item_order,
            limit=fetch_limit,
            offset=0,
            status_user_id=user_id,
        )

        cluster_total_row = conn.execute(
            f"""SELECT count(*) AS n
                  FROM {schema}.cluster_status s
                  JOIN {schema}.clusters c ON c.id = s.cluster_id
                 WHERE s.user_id = %(user_id)s
                   AND {cluster_status_predicate}
                   AND COALESCE(c.archived, false) = false
                   AND c.merged_into IS NULL
                   {cluster_privacy_filter}""",
            cluster_params,
        ).fetchone()
        cluster_rows = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version,
                       c.is_visible_in_feed, cat.category,
                       s.clicked_at, s.starred_at, s.last_seen_version
                  FROM {schema}.cluster_status s
                  JOIN {schema}.clusters c ON c.id = s.cluster_id
                  LEFT JOIN LATERAL (
                    SELECT i.cover_url
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.cover_url, '') IS NOT NULL
                     ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                              ci.rank_in_cluster ASC NULLS LAST
                     LIMIT 1
                  ) detail_cover ON true
                  LEFT JOIN LATERAL (
                    SELECT i.ai_category AS category
                      FROM {schema}.cluster_items ci
                      JOIN {schema}.items i ON i.id = ci.item_id
                     WHERE ci.cluster_id = c.id
                       AND NULLIF(i.ai_category, '') IS NOT NULL
                     GROUP BY i.ai_category
                     ORDER BY count(*) DESC
                     LIMIT 1
                  ) cat ON true
                 WHERE s.user_id = %(user_id)s
                   AND {cluster_status_predicate}
                   AND COALESCE(c.archived, false) = false
                   AND c.merged_into IS NULL
                   {cluster_privacy_filter}
                 ORDER BY s.{status_field} DESC NULLS LAST
                 LIMIT %(limit)s""",
            cluster_params,
        ).fetchall()

    entries = (
        [_library_item_entry(item, status_field=status_field) for item in item_rows]
        + [_library_cluster_entry(dict(row), status_field=status_field) for row in cluster_rows]
    )
    entries.sort(key=lambda entry: entry.get("occurred_at") or "", reverse=True)
    total = int(item_total_row["n"] if item_total_row else 0) + int(cluster_total_row["n"] if cluster_total_row else 0)
    return {
        "entries": entries[offset:offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "view": view,
        "data_backend": feed_read_backend(),
    }


def query_feed(
    *,
    platform: str | None = None,
    source: str | None = None,
    unread: bool = False,
    starred: bool = False,
    clicked: bool = False,
    search: str | None = None,
    limit: int = 0,
    offset: int = 0,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    """Return `/api/feed` data from the remote DB.

    Anonymous requests do not have a remote item_status scope. Starred/clicked
    filters therefore return empty results, while unread behaves like "all".
    """
    if not user_id and (starred or clicked):
        return {"items": [], "total": 0, "offset": offset, "limit": limit, "data_backend": feed_read_backend()}
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    _add_search_filter(where, params, search)
    if platform:
        where.append("i.platform = %(platform)s")
        params["platform"] = platform
    if source:
        where.append("i.source = %(source)s")
        params["source"] = source
    order_sql = "ORDER BY i.fetched_at DESC NULLS LAST, i.published_at DESC NULLS LAST"
    if user_id:
        if unread:
            where.append("(s.item_id IS NULL OR (s.clicked_at IS NULL AND s.hidden_at IS NULL))")
        if starred:
            where.append("s.starred_at IS NOT NULL")
        if clicked:
            where.append("s.clicked_at IS NOT NULL")
            order_sql = "ORDER BY s.clicked_at DESC NULLS LAST"
    count_cache_key = (
        "feed_total",
        schema,
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        platform or "",
        source or "",
        search or "",
        bool(unread),
        bool(starred),
        bool(clicked),
    )
    local_cache_name = None
    if (
        not user_id
        and not platform
        and not source
        and not search
        and not unread
        and not starred
        and not clicked
        and offset == 0
        and 0 < int(limit or 0) <= 50
        and manual_owner_user_id is None
    ):
        local_cache_name = _feed_items_local_cache_name(
            limit=limit,
            public_only=public_only,
            min_github_stars=min_github_stars,
        )
        fresh_fallback = _read_local_read_cache(
            local_cache_name,
            max_age_sec=_LOCAL_READ_CACHE_FRESH_SEC,
        )
        if fresh_fallback is not None:
            return fresh_fallback

    if _remote_feed_live_circuit_open():
        if local_cache_name:
            stale_fallback = _read_local_read_cache(local_cache_name)
            if stale_fallback is not None:
                return _mark_stale_payload(stale_fallback, source="local_read_cache")
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "data_backend": feed_read_backend(),
            "degraded": True,
        }

    try:
        with connect() as conn:
            _set_short_statement_timeout(
                conn,
                _remote_feed_search_timeout_ms() if search else _remote_feed_live_timeout_ms(),
            )
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=order_sql,
                limit=limit if limit > 0 else None,
                offset=offset,
                status_user_id=user_id,
            )
    except Exception as exc:
        if search:
            # BF-0704-6: 搜索失败只降级搜索本身,不熔断整个 feed live 读
            print(f"[warn] feed search query failed (search={search!r}): {exc}")
        else:
            _mark_remote_feed_live_circuit_open()
        if local_cache_name:
            stale_fallback = _read_local_read_cache(local_cache_name)
            if stale_fallback is not None:
                return _mark_stale_payload(stale_fallback, source="local_read_cache")
        return {
            "items": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "data_backend": feed_read_backend(),
            "degraded": True,
        }

    total = _cache_get(count_cache_key)
    total_is_estimate = False
    if total is None:
        try:
            with connect() as conn:
                _set_short_statement_timeout(
                    conn,
                    _remote_feed_search_timeout_ms() if search else _remote_feed_live_timeout_ms(),
                )
                count_params = dict(params)
                count_join, count_status_params, _ = _item_status_join(schema, user_id)
                count_params.update(count_status_params)
                if search:
                    # 搜索 total 封顶,避免全量匹配行回表 count
                    count_params["count_cap"] = CONTEXT_SEARCH_EVENTS_TOTAL_CAP
                    count_sql = (
                        f"SELECT count(*) AS n FROM (SELECT 1 FROM {schema}.items i "
                        f"{count_join} {_where_sql(where)} LIMIT %(count_cap)s) capped"
                    )
                else:
                    count_sql = f"SELECT count(*) AS n FROM {schema}.items i {count_join} {_where_sql(where)}"
                total_row = conn.execute(count_sql, count_params).fetchone()
                total = int(total_row["n"] if total_row else 0)
                _cache_set(count_cache_key, total)
        except Exception as exc:
            if search:
                print(f"[warn] feed search count failed (search={search!r}): {exc}")
            else:
                _mark_remote_feed_live_circuit_open()
            total = max(0, offset) + len(items)
            total_is_estimate = True
    result = {
        "items": items,
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "data_backend": feed_read_backend(),
    }
    if total_is_estimate:
        result["degraded"] = True
        result["degraded_reason"] = "feed_total_unavailable"
        result["total_is_estimate"] = True
    elif local_cache_name:
        _write_local_read_cache_async(local_cache_name, result)
    return result


def context_search(
    *,
    q: str,
    context: str = "recommend",
    limit: int = 30,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
    categories: list[str] | None = None,
    events_only: bool = False,
) -> dict:
    """Remote-only implementation for `/api/search`.

    Search remains full-DB semantics: the DB computes totals, and the API only
    returns the first page of docs/events for rendering.
    """
    keyword = (q or "").strip()
    if not keyword:
        base: dict[str, Any] = {"docs": [], "docs_total": 0}
        if context == "recommend":
            base.update({"events": [], "events_total": 0})
        return base

    events_only_recommend = events_only and context == "recommend"
    if events_only_recommend:
        out: dict[str, Any] = {"docs": [], "docs_total": 0}
    else:
        docs_body = query_feed(
            search=keyword,
            limit=limit,
            offset=0,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        out = {
            "docs": docs_body["items"],
            "docs_total": docs_body["total"],
        }
    if context != "recommend":
        return out

    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    github_filter = _github_display_filter(schema, min_github_stars, "c")
    categories_filter = ""
    if categories:
        categories_filter = f"""
          AND EXISTS (
            SELECT 1
            FROM {schema}.cluster_items ci2
            JOIN {schema}.items i2 ON i2.id = ci2.item_id
            WHERE ci2.cluster_id = c.id
              AND split_part(coalesce(i2.ai_category, ''), '[', 1) = ANY(%(categories)s::text[])
          )
        """
    params = {"search_like": f"%{keyword}%", "limit": limit}
    base_filters_sql = f"""
      c.is_visible_in_feed = true
      AND c.published_at IS NOT NULL
      AND coalesce(c.archived, false) = false
      AND c.merged_into IS NULL
      {public_filter}
      {github_filter}
      {categories_filter}
    """
    # BF-0704-6 rev2: 标题优先。concat(title+summary) 的 bitmap recheck 要对每个
    # 匹配行 detoast ai_summary(高频词 2k+ 行,冷缓存 >15s);ai_title 是行内列,
    # recheck 零 TOAST 回表。标题命中不足一页时才用全文摘要补充(稀有词匹配行少)。
    title_where_sql = f"{base_filters_sql} AND c.ai_title ILIKE %(search_like)s"
    supplement_where_sql = (
        f"{base_filters_sql}"
        " AND (coalesce(c.ai_title, '') || ' ' || coalesce(c.ai_summary, '')) ILIKE %(search_like)s"
        " AND NOT (c.ai_title ILIKE %(search_like)s)"
    )
    params["categories"] = categories or []
    cache_key = (
        "context_search_events_total",
        schema,
        keyword,
        bool(public_only),
        user_id or "",
        int(min_github_stars),
        tuple(categories or []),
    )
    degraded_cache_key = (
        "context_search_events_degraded",
        schema,
        keyword,
        bool(public_only),
        user_id or "",
        int(min_github_stars),
        tuple(categories or []),
    )
    if events_only_recommend and _cache_get_with_ttl(
        degraded_cache_key,
        CONTEXT_SEARCH_EVENTS_DEGRADED_TTL_SEC,
    ):
        out["events"] = []
        out["events_total"] = 0
        out["data_backend"] = event_read_backend()
        out["degraded"] = True
        out["degraded_reason"] = "context_search_events_unavailable"
        return out
    def _events_search_sql(where_clause: str) -> str:
        return f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.doc_count,
                           c.unique_source_count, c.first_doc_at, c.last_doc_at,
                           c.platforms_json,
                           COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                           c.live_version
                      FROM {schema}.clusters c
                      LEFT JOIN LATERAL (
                        SELECT i.cover_url
                          FROM {schema}.cluster_items ci
                          JOIN {schema}.items i ON i.id = ci.item_id
                         WHERE ci.cluster_id = c.id
                           AND NULLIF(i.cover_url, '') IS NOT NULL
                           AND i.platform <> 'manual'
                           AND i.user_id IS NULL
                         ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                                  ci.rank_in_cluster ASC NULLS LAST
                         LIMIT 1
                      ) event_cover ON true
                     WHERE {where_clause}
                     ORDER BY c.first_doc_at DESC NULLS LAST,
                              c.last_updated_at DESC NULLS LAST,
                              c.id DESC
                     LIMIT %(limit)s"""

    try:
        with connect() as conn:
            statement_timeout_ms = (
                _context_search_events_only_statement_timeout_ms()
                if events_only_recommend
                else None
            )
            if not _set_context_search_timeouts(conn, statement_timeout_ms=statement_timeout_ms):
                raise RemoteDBError("context search timeout setup failed")
            rows = list(conn.execute(_events_search_sql(title_where_sql), params).fetchall())
            supplement_rows: list = []
            if len(rows) < limit:
                # 标题命中不足一页 → 稀有词,全文摘要补充便宜;补充失败只丢补充,不整体降级
                try:
                    supplement_params = dict(params)
                    supplement_params["limit"] = limit - len(rows)
                    supplement_rows = list(
                        conn.execute(
                            _events_search_sql(supplement_where_sql), supplement_params
                        ).fetchall()
                    )
                except Exception as supp_exc:
                    print(
                        f"[warn] context search summary supplement failed (q={keyword!r}): {supp_exc}"
                    )
                    conn.rollback()
                    if not _set_context_search_timeouts(
                        conn, statement_timeout_ms=statement_timeout_ms
                    ):
                        raise RemoteDBError("context search timeout setup failed")
            if supplement_rows:
                rows = rows + supplement_rows
                # 两段结果各自有序,合并后按同一时间线键重排(ISO 字符串可比,规避 naive/aware 混排)
                rows.sort(
                    key=lambda r: (to_utc_iso(r["first_doc_at"]) or "", int(r["id"])),
                    reverse=True,
                )
                rows = rows[:limit]
            total = _cache_get(cache_key)
            if total is None:
                # BF-0704-6: total 只做展示。封顶 count 且只按标题算(行内列,零 TOAST),
                # 补充命中数直接累加;全量 concat count 冷缓存要回表数千行,不再使用。
                count_params = dict(params)
                count_params["count_cap"] = CONTEXT_SEARCH_EVENTS_TOTAL_CAP
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM (SELECT 1 FROM {schema}.clusters c "
                    f"WHERE {title_where_sql} LIMIT %(count_cap)s) capped",
                    count_params,
                ).fetchone()
                total = int(total_row["n"] if total_row else 0)
                if total < CONTEXT_SEARCH_EVENTS_TOTAL_CAP:
                    total += len(supplement_rows)
                _cache_set(cache_key, total)
            source_metadata = _fetch_event_source_metadata(conn, schema, [int(row["id"]) for row in rows])
    except Exception as exc:
        print(f"[warn] context search events query failed (q={keyword!r}): {exc}")
        if events_only_recommend:
            _cache_set_with_ttl(
                degraded_cache_key,
                True,
                CONTEXT_SEARCH_EVENTS_DEGRADED_TTL_SEC,
            )
        out["events"] = []
        out["events_total"] = 0
        out["data_backend"] = event_read_backend()
        out["degraded"] = True
        out["degraded_reason"] = "context_search_events_unavailable"
        return out
    out["events"] = [_row_to_event(dict(row), source_metadata=source_metadata) for row in rows]
    out["events_total"] = int(total)
    return out




def _can_use_sections_mv_fast_path(
    *,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
) -> bool:
    if user_id is not None or search or manual_owner_user_id:
        return False
    if per_category is None:
        return False
    try:
        return int(per_category) <= 50
    except (TypeError, ValueError):
        return False


def _feed_sections_local_cache_name(
    *,
    per_category: int | None,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return f"feed_sections_per={per_category or 'all'}_public={int(public_only)}_stars={int(min_github_stars)}"


def _section_category_from_row(row: dict[str, Any]) -> str:
    categories = [
        str(cat).strip()
        for cat in _json_array(row.get("ai_categories"))
        if str(cat).strip()
    ]
    if categories:
        return categories[0]
    single = canonicalize_category(row.get("ai_category"))
    if single and single != "other":
        return single
    return "_uncategorized"


def _sections_from_mv_rows(
    rows: list[Any],
    *,
    per_category: int | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    sections: dict[str, list[dict[str, Any]]] = {}
    cat_counts: dict[str, int] = {}
    limit = max(1, int(per_category)) if per_category is not None else None

    for row in rows:
        raw = dict(row)
        category = _section_category_from_row(raw)
        cat_counts[category] = cat_counts.get(category, 0) + 1
        bucket = sections.setdefault(category, [])
        if limit is None or len(bucket) < limit:
            raw["section_category"] = category
            bucket.append(_normalize_item(raw))
    return sections, cat_counts


def _section_counts_from_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
    category_expr: str,
) -> dict[str, int]:
    rows = conn.execute(
        f"""SELECT {category_expr} AS section_category, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY 1
             ORDER BY cnt DESC""",
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        data = dict(row)
        category = data.get("section_category") or data.get("category") or "_uncategorized"
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            counts[category] = cnt
    return counts


def query_feed_sections(
    *,
    per_category: int | None = 50,
    search: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 Spec-2.5（rev1, 2026-05-15）: 与 query_feed_platforms 同一份双字段
    # OR AI 过滤口径（D3 决策），保证「按频道」/「按分类」两个视角看到同一批
    # 数据。原 `ai_categories IS NOT NULL` 单字段严格过滤会丢长尾 multi-tag
    # 数据 + 与 platforms 入口口径不一致；改成 OR 后分组键 fallback 到
    # ai_category 单字段（COALESCE 表达式扩展处理）。
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    # v18.0 Spec-2.5: 分组键优先 multi-tag ai_categories[0]，缺失时 fallback
    # 到单字段 ai_category（OR 过滤后允许此分支），仍空兜底 _uncategorized。
    category_expr = _section_category_expr("i")
    # perf-v27 P3: live overlay 已删——读模型刷新绑定抓取(≤15min 旧),
    # 不再需要请求时现场补新鲜(1500ms 超时反复失败正是两次冻结事故的级联症状)。
    result_cache_ttl = _feed_result_cache_ttl_sec()
    result_cache_key = (
        "feed_sections_result",
        schema,
        per_category,
        search or "",
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
    )
    cached_result = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
    if cached_result is not None:
        return cached_result
    def _compute_read_model() -> dict[str, Any] | None:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        if search:
            return _query_feed_sections_search_read_model(
                schema=schema,
                per_category=per_category,
                search=search,
                user_id=user_id,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
        return _query_feed_sections_read_model(
            schema=schema,
            per_category=per_category,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )

    read_model_result = _singleflight_sync(
        ("feed_sections_read_model", *result_cache_key),
        _compute_read_model,
    )
    if read_model_result is not None:
        cache_ttl = _feed_result_cache_ttl(read_model_result)
        if cache_ttl > 0:
            return _cache_set_copy_with_ttl(result_cache_key, read_model_result, cache_ttl)
        return read_model_result
    if search and _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return _degraded_feed_sections_result("info_search_read_model_unavailable")
    use_sections_fast_path = _can_use_sections_mv_fast_path(
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    )
    if _remote_feed_live_circuit_open():
        if use_sections_fast_path:
            fallback = _read_local_read_cache(
                _feed_sections_local_cache_name(
                    per_category=per_category,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
        return _degraded_feed_sections_result()
    # BF-0515-singleflight: dedupe concurrent cache-miss → only 1 thread queries Supabase.
    # Other concurrent callers wait on threading.Event and share the same result.
    # Re-check cache inside compute_fn in case singleflight wait yielded a winner.
    def _compute() -> dict:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        snapshot_key = None
        if search is None and not user_id:
            snapshot_key = _sections_snapshot_key(
                per_category=per_category,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                min_github_stars=min_github_stars,
            )
        live_conn = None
        try:
            with connect() as conn:
                live_conn = conn
                _set_short_statement_timeout(conn, _remote_feed_live_timeout_ms())
                item_params = dict(params)
                status_join, status_params, status_alias = _item_status_join(schema, user_id)
                item_params.update(status_params)
                limit_sql = ""
                if per_category is not None:
                    item_params["per_category"] = max(1, int(per_category))
                    limit_sql = "WHERE rn <= %(per_category)s"
                rows = conn.execute(
                    f"""WITH ranked AS (
                           SELECT {_feed_cols(status_alias)},
                                  {category_expr} AS section_category,
                                  row_number() OVER (
                                    PARTITION BY {category_expr}
                                    ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                             i.fetched_at DESC NULLS LAST,
                                             i.relevance_score DESC NULLS LAST
                                  ) AS rn
                             FROM {schema}.items i
                             {status_join}
                             {_where_sql(where)}
                         )
                         SELECT * FROM ranked
                         {limit_sql}
                         ORDER BY section_category,
                                  COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                  fetched_at DESC NULLS LAST,
                                  relevance_score DESC NULLS LAST""",
                    item_params,
                ).fetchall()
                sections = {}
                for row in rows:
                    raw = dict(row)
                    category = raw.get("section_category") or "_uncategorized"
                    sections.setdefault(category, []).append(_normalize_item(raw))
                cat_counts = _section_counts_from_items(conn, schema, where, params, category_expr)
                result = {
                    "sections": sections,
                    "total": sum(cat_counts.values()),
                    "cat_counts": cat_counts,
                    "personalized": False,
                    "data_backend": feed_read_backend(),
                    "overview_generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sample_limit": int(per_category) if per_category is not None else None,
                }
                if snapshot_key:
                    _write_local_read_cache_async(
                        _feed_sections_local_cache_name(
                            per_category=per_category,
                            public_only=public_only,
                            min_github_stars=min_github_stars,
                        ),
                        result,
                    )
        except Exception:
            if live_conn is not None:
                _rollback_safely(live_conn)
            raise RemoteDBError("sections live path failed")
        return _cache_set_copy(result_cache_key, result)

    try:
        return _singleflight_sync(result_cache_key, _compute)
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        if use_sections_fast_path:
            fallback = _read_local_read_cache(
                _feed_sections_local_cache_name(
                    per_category=per_category,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
            return _degraded_feed_sections_result()
        return _degraded_feed_sections_result()


def query_feed_by_category(
    *,
    category: str,
    keyword: str | None = None,
    search: str | None = None,
    subcategory: str | None = None,
    offset: int = 0,
    limit: int = 50,
    cursor: dict[str, Any] | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    if not keyword and not search and manual_owner_user_id:
        union_page = _query_feed_by_category_private_manual_union_page(
            schema=schema,
            category=category,
            subcategory=subcategory,
            offset=safe_offset,
            limit=safe_limit,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if union_page is not None:
            return union_page
    if search:
        search_read_model_result = _query_feed_by_category_search_read_model(
            schema=schema,
            category=category,
            keyword=keyword,
            search=search,
            subcategory=subcategory,
            offset=safe_offset,
            limit=safe_limit,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if search_read_model_result is not None:
            return search_read_model_result
        if (
            not keyword
            # ENG-0710: 子板块搜索 scope 已不物化,该组合放行到下方 live(带独立预算)
            and not subcategory
            and not manual_owner_user_id
            and _can_use_info_search_read_model(
                search=search,
                user_id=user_id,
                public_only=public_only,
                min_github_stars=min_github_stars,
            )
        ):
            return _degraded_feed_category_result(
                category,
                "info_search_read_model_unavailable",
            )
    read_model_result = _query_feed_by_category_read_model(
        schema=schema,
        category=category,
        keyword=keyword,
        search=search,
        subcategory=subcategory,
        offset=safe_offset,
        limit=safe_limit,
        cursor=cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    if read_model_result is not None:
        return read_model_result
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    if category == UNCATEGORIZED_SENTINEL:
        params["category"] = UNCATEGORIZED_SENTINEL
        where.append(f"{category_expr} = %(category)s")
    else:
        category_ids = expand_query_categories(category)
        params["category_ids"] = category_ids
        where.append(f"{category_expr} = ANY(%(category_ids)s)")
    _add_search_filter(where, params, search, param_key="global_search_like")
    _add_search_filter(where, params, keyword, param_key="keyword_search_like")
    if subcategory:
        # ENG-0710: @> 走 GIN(jsonb_path_ops) 索引;SRF 展开形态实测 11.75s 必超预算
        params["subcategory_arr"] = json.dumps([subcategory])
        where.append("i.ai_subcategories @> %(subcategory_arr)s::jsonb")
    where.append("i.visible = 1")
    _add_ai_relevance_filter(where)
    count_cache_key = (
        "feed_category_count",
        schema,
        category,
        keyword or "",
        search or "",
        subcategory or "",
        bool(public_only),
        manual_owner_user_id or "",
        user_id or "",
        int(min_github_stars),
    )
    live_blocked = (
        _remote_feed_live_runtime_circuit_open()
        if subcategory
        else _remote_feed_live_circuit_open()
    )
    if live_blocked:
        return _degraded_feed_category_result(category)
    live_timeout_ms = _remote_feed_live_timeout_ms()
    if subcategory:
        # 子板块视图不再物化、只此一条路;热门子板块冷缓存 @> count 实测 ~4s,
        # 给独立更宽预算(请求级 30s 墙钟仍兜底),count 命中缓存后秒回
        live_timeout_ms = max(
            live_timeout_ms,
            _env_int(_runtime_env(), SUBCATEGORY_LIVE_TIMEOUT_MS_ENV, 10000, min_value=1000),
        )
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, live_timeout_ms)
            count = _cache_get(count_cache_key)
            if count is None:
                count_params = dict(params)
                count_join, count_status_params, _ = _item_status_join(schema, user_id)
                count_params.update(count_status_params)
                total_row = conn.execute(
                    f"SELECT count(*) AS n FROM {schema}.items i {count_join} {_where_sql(where)}",
                    count_params,
                ).fetchone()
                count = int(total_row["n"] if total_row else 0)
                _cache_set(count_cache_key, count)
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=safe_offset,
                status_user_id=user_id,
            )
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        return _degraded_feed_category_result(category)
    return {
        "items": items,
        "category": category,
        "total": int(count),
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": safe_offset + len(items) < int(count),
        "next_offset": (
            safe_offset + len(items)
            if safe_offset + len(items) < int(count)
            else None
        ),
        "data_backend": feed_read_backend(),
    }


def _category_counts_for_all_platforms(
    conn: Any,
    schema: str,
    *,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    search: str | None = None,
) -> dict[str, dict[str, int]]:
    cache_key = (
        "platform_category_counts",
        schema,
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
        search or "",
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 PRD §Spec-2: category counts 必须与 query_feed_platforms 同口径
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    cat_rows = conn.execute(
        f"""SELECT i.platform, cat.value AS category, count(DISTINCT i.id) AS cnt
              FROM {schema}.items i
              CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
              {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
             GROUP BY i.platform, cat.value
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    null_rows = conn.execute(
        f"""SELECT i.platform, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where + ["i.ai_categories IS NULL"])}
             GROUP BY i.platform""",
        params,
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in cat_rows:
        platform = row["platform"] or "_unknown"
        counts.setdefault(platform, {})[row["category"]] = int(row["cnt"] or 0)
    for row in null_rows:
        platform = row["platform"] or "_unknown"
        cnt = int(row["cnt"] or 0)
        if cnt > 0:
            counts.setdefault(platform, {})[UNCATEGORIZED_SENTINEL] = cnt
    return _cache_set(cache_key, counts)


def _can_use_platforms_mv_fast_path(
    *,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
) -> bool:
    """Return whether /api/feed/platforms can use the MV-only first paint path."""
    if user_id is not None or search or manual_owner_user_id:
        return False
    if per_platform is None:
        return False
    try:
        return int(per_platform) <= 50
    except (TypeError, ValueError):
        return False


def _feed_platforms_local_cache_name(
    *,
    per_platform: int | None,
    public_only: bool,
    min_github_stars: int,
) -> str:
    return f"feed_platforms_per={per_platform or 'all'}_public={int(public_only)}_stars={int(min_github_stars)}"


def _with_degraded_reason(result: dict[str, Any], reason: str | None) -> dict[str, Any]:
    if reason:
        result["degraded_reason"] = reason
    return result


def _degraded_feed_platforms_result(reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "sections": {},
        "platform_counts": {},
        "source_counts": {},
        "category_counts": {},
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_sections_result(reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "sections": {},
        "total": 0,
        "cat_counts": {},
        "personalized": False,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_category_result(category: str, reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "items": [],
        "category": category,
        "total": 0,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _degraded_feed_platform_page_result(platform: str, *, category: str | None = None, reason: str | None = None) -> dict[str, Any]:
    return _with_degraded_reason({
        "items": [],
        "platform": platform,
        "category": category,
        "total": 0,
        "data_backend": feed_read_backend(),
        "degraded": True,
    }, reason)


def _platform_overview_counts_from_rows(
    rows: list[Any],
) -> tuple[
    dict[str, int],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """Derive lightweight overview counts from MV rows.

    This intentionally counts only the precomputed top rows. It keeps the
    anonymous information page usable even when full-table remote aggregations
    are slow or stuck.
    """
    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

        source = data.get("source") or ""
        platform_source_counts = source_counts.setdefault(platform, {})
        platform_source_counts[source] = platform_source_counts.get(source, 0) + 1

        categories = [
            str(cat).strip()
            for cat in _json_array(data.get("ai_categories"))
            if str(cat).strip()
        ]
        if not categories:
            single = canonicalize_category(data.get("ai_category"))
            if single and single != "other":
                categories = [single]
            else:
                categories = [UNCATEGORIZED_SENTINEL]
        for category in categories:
            platform_category_counts = category_counts.setdefault(platform, {})
            platform_category_counts[category] = platform_category_counts.get(category, 0) + 1
    return platform_counts, source_counts, category_counts


def _count_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
) -> int:
    row = conn.execute(
        f"SELECT count(*) AS n FROM {schema}.items i {_where_sql(where)}",
        params,
    ).fetchone()
    return int((row or {}).get("n") or 0)


def _platform_page_count_cache_key(
    *,
    schema: str,
    platform: str,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
    search: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> tuple[Any, ...]:
    return (
        "feed_platform_page_count",
        schema,
        platform,
        source or "",
        group or "",
        category or "",
        search or "",
        bool(public_only),
        manual_owner_user_id or "",
        user_id or "",
        int(min_github_stars),
    )


def _warm_platform_page_count_cache(
    *,
    schema: str,
    platform_counts: dict[str, int],
    source_counts: dict[str, dict[str, int]],
    category_counts: dict[str, dict[str, int]],
    search: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    user_id: str | None,
    min_github_stars: int,
) -> None:
    for platform, total in platform_counts.items():
        _cache_set(
            _platform_page_count_cache_key(
                schema=schema,
                platform=platform,
                search=search,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                user_id=user_id,
                min_github_stars=min_github_stars,
            ),
            int(total),
        )
    for platform, per_source in source_counts.items():
        for source, total in per_source.items():
            _cache_set(
                _platform_page_count_cache_key(
                    schema=schema,
                    platform=platform,
                    source=source,
                    search=search,
                    public_only=public_only,
                    manual_owner_user_id=manual_owner_user_id,
                    user_id=user_id,
                    min_github_stars=min_github_stars,
                ),
                int(total),
            )
    for platform, per_category in category_counts.items():
        for category, total in per_category.items():
            _cache_set(
                _platform_page_count_cache_key(
                    schema=schema,
                    platform=platform,
                    category=category,
                    search=search,
                    public_only=public_only,
                    manual_owner_user_id=manual_owner_user_id,
                    user_id=user_id,
                    min_github_stars=min_github_stars,
                ),
                int(total),
            )


def _estimate_platform_page_total(*, offset: int, limit: int, item_count: int) -> int:
    loaded_until = offset + item_count
    if item_count >= limit:
        return loaded_until + 1
    return loaded_until


def _platform_overview_counts_from_items(
    conn: Any,
    schema: str,
    where: list[str],
    params: dict[str, Any],
) -> tuple[
    dict[str, int],
    dict[str, dict[str, int]],
    dict[str, dict[str, int]],
]:
    """Aggregate full overview counts from the live items table.

    The cards query can stay capped at 50 per platform, but these counts are
    user-facing totals and must not be derived from that capped card sample.
    """
    platform_rows = conn.execute(
        f"""SELECT i.platform, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY i.platform
             ORDER BY cnt DESC""",
        params,
    ).fetchall()
    source_rows = conn.execute(
        f"""SELECT i.platform, i.source, count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where)}
             GROUP BY i.platform, i.source
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    category_rows = conn.execute(
        f"""SELECT i.platform, cat.value AS category, count(DISTINCT i.id) AS cnt
              FROM {schema}.items i
              CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
              {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
             GROUP BY i.platform, cat.value
             ORDER BY i.platform, cnt DESC""",
        params,
    ).fetchall()
    null_category_rows = conn.execute(
        f"""SELECT i.platform,
                   CASE
                     WHEN i.ai_category IS NOT NULL AND i.ai_category != 'other'
                     THEN i.ai_category
                     ELSE %(uncategorized)s
                   END AS category,
                   count(*) AS cnt
              FROM {schema}.items i
              {_where_sql(where + ["i.ai_categories IS NULL"])}
             GROUP BY i.platform, category
             ORDER BY i.platform, cnt DESC""",
        {**params, "uncategorized": UNCATEGORIZED_SENTINEL},
    ).fetchall()

    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}

    for row in platform_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            platform_counts[platform] = cnt

    for row in source_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        source = data.get("source") or ""
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            source_counts.setdefault(platform, {})[source] = cnt

    for row in list(category_rows) + list(null_category_rows):
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            category_counts.setdefault(platform, {})[category] = (
                category_counts.setdefault(platform, {}).get(category, 0) + cnt
            )

    return platform_counts, source_counts, category_counts


def _section_category_expr(item_alias: str = "i") -> str:
    """Primary category expression for the 信息 tab type sections."""
    alias = item_alias.strip() or "i"
    raw = (
        "COALESCE("
        f"{alias}.ai_categories ->> 0,"
        f" CASE WHEN {alias}.ai_category IS NOT NULL AND {alias}.ai_category != 'other'"
        f" THEN {alias}.ai_category ELSE NULL END,"
        f" '{UNCATEGORIZED_SENTINEL}'"
        ")"
    )
    return (
        "CASE "
        f"WHEN {raw} IN ('ai_tools', 'tools') THEN 'efficiency_tools' "
        f"WHEN {raw} = 'insights' THEN 'tech' "
        f"ELSE {raw} "
        "END"
    )


def _max_fetched_at(items: list[dict[str, Any]]) -> str | None:
    timestamps = [item.get("fetched_at") for item in items if item.get("fetched_at")]
    if not timestamps:
        return None
    return max(timestamps, key=sort_key)


def _info_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(INFO_READ_MODEL_ENV))


def _info_scope_key(*, platform: str, dimension: str, value: str | None = None) -> str:
    return f"platform={platform}|dimension={dimension}|value={value or ''}"


INFO_SCOPE_COMPOUND_SEPARATOR = "::"


def _info_section_subcategory_value(category: str, subcategory: str) -> str:
    return f"{category}{INFO_SCOPE_COMPOUND_SEPARATOR}{subcategory}"


def _split_info_compound_value(value: str) -> tuple[str, str]:
    left, sep, right = str(value or "").partition(INFO_SCOPE_COMPOUND_SEPARATOR)
    return left, right if sep else ""


def _info_read_model_page_cache_key(
    *,
    schema: str,
    platform: str,
    source: str | None,
    group: str | None,
    category: str | None,
    offset: int,
    limit: int,
    exclude_ids: list[str] | None = None,
    version_id: str | None = None,
) -> tuple[Any, ...]:
    return (
        "info_read_model_platform_page",
        schema,
        version_id or "",
        platform,
        source or "",
        group or "",
        category or "",
        int(offset),
        int(limit),
        tuple(exclude_ids or []),
    )


def _info_read_model_section_category_page_cache_key(
    *,
    schema: str,
    category: str,
    subcategory: str | None = None,
    offset: int,
    limit: int,
    version_id: str | None = None,
) -> tuple[Any, ...]:
    return (
        "info_read_model_section_category_page",
        schema,
        version_id or "",
        category,
        subcategory or "",
        int(offset),
        int(limit),
    )


def _normalize_info_read_model_cursor(
    cursor: Any,
    *,
    expected_scope_key: str,
) -> dict[str, Any] | None:
    if not isinstance(cursor, dict):
        return None
    version_id = str(cursor.get("version_id") or "").strip()
    scope_key = str(cursor.get("scope_key") or "").strip()
    if not version_id or scope_key != expected_scope_key:
        return None
    try:
        rank_after = int(cursor.get("rank_after"))
    except (TypeError, ValueError):
        return None
    if rank_after < 0:
        return None
    clean_exclude_ids: list[str] = []
    for raw_item_id in cursor.get("exclude_ids") or []:
        item_id = str(raw_item_id or "").strip()
        if item_id and item_id not in clean_exclude_ids:
            clean_exclude_ids.append(item_id)
        if len(clean_exclude_ids) >= 200:
            break
    normalized = {
        "version_id": version_id,
        "scope_key": scope_key,
        "rank_after": rank_after,
    }
    if clean_exclude_ids:
        normalized["exclude_ids"] = clean_exclude_ids
    return normalized


def _info_read_model_next_cursor(
    *,
    version_id: Any,
    scope_key: str,
    rank_after: int,
    total_count: int,
    exclude_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    if not version_id or not scope_key:
        return None
    try:
        rank_value = max(0, int(rank_after))
        total_value = max(0, int(total_count))
    except (TypeError, ValueError):
        return None
    if rank_value >= total_value:
        return None
    clean_exclude_ids: list[str] = []
    for raw_item_id in exclude_ids or []:
        item_id = str(raw_item_id or "").strip()
        if item_id and item_id not in clean_exclude_ids:
            clean_exclude_ids.append(item_id)
        if len(clean_exclude_ids) >= 200:
            break
    cursor = {
        "version_id": str(version_id),
        "scope_key": scope_key,
        "rank_after": rank_value,
    }
    if clean_exclude_ids:
        cursor["exclude_ids"] = clean_exclude_ids
    return cursor


def _item_ids_for_cursor(items: list[dict[str, Any]]) -> list[str]:
    item_ids: list[str] = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id not in item_ids:
            item_ids.append(item_id)
        if len(item_ids) >= 200:
            break
    return item_ids


def _info_platform_scope(
    *,
    platform: str,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
) -> tuple[str, str, str] | None:
    if source and group and not category:
        value = _info_group_source_value(str(group), str(source))
        return "group_source", value, _info_scope_key(platform=platform, dimension="group_source", value=value)
    active_filters = [
        ("source", source),
        ("group", group),
        ("category", category),
    ]
    chosen = [(dimension, value) for dimension, value in active_filters if value]
    if len(chosen) > 1:
        return None
    if not chosen:
        dimension, value = "all", ""
    else:
        dimension, value = chosen[0]
    return dimension, str(value or ""), _info_scope_key(platform=platform, dimension=dimension, value=value)


def _can_use_info_read_model(
    *,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> bool:
    if not _info_read_model_enabled():
        return False
    if search:
        return False
    if not public_only and not user_id:
        return False
    return int(min_github_stars) == INFO_READ_MODEL_MIN_GITHUB_STARS


def _can_use_info_search_read_model(
    *,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    min_github_stars: int,
) -> bool:
    # perf-v27 P4: 恒 False——搜索统一改走 live(items trgm 索引,覆盖 90 天
    # 全保留期)。读模型 card_items 已收缩到 7 天热窗口,继续在卡片上搜会把
    # 搜索覆盖面静默砍到 7 天(违反"搜 OpenAI 出全部历史"的产品承诺)。
    # gate 关闭后:4 个 *_search_read_model 函数早退 None、各入口的
    # "search read model unavailable" 降级短路全部跳过 → 既有 live 搜索段
    # (自带预算+熔断)接管。函数保留作路由地标;函数体清理列入 backlog。
    return False


def _read_model_card_search_sql(
    *,
    card_expr: str = "ci.card_json",
    param_key: str = "search_like",
    search_text_expr: str | None = None,
) -> str:
    if search_text_expr:
        return f"{search_text_expr} ILIKE %({param_key})s"
    return (
        f"(coalesce({card_expr} ->> 'title', '') || ' ' || "
        f"coalesce({card_expr} ->> 'author_name', '') || ' ' || "
        f"coalesce({card_expr} ->> 'description', '') || ' ' || "
        f"coalesce({card_expr} ->> 'ai_summary', '') || ' ' || "
        f"coalesce({card_expr} ->> 'ai_keywords', '')) ILIKE %({param_key})s"
    )


def _private_manual_base_where(
    *,
    manual_owner_user_id: str,
    search: str | None = None,
    min_github_stars: int = 50,
) -> tuple[list[str], dict[str, Any]]:
    where = [
        "i.platform = 'manual'",
        "i.user_id = %(manual_owner_user_id)s",
        "i.visible = 1",
    ]
    params: dict[str, Any] = {"manual_owner_user_id": manual_owner_user_id}
    where.extend(_item_display_filter("i", min_github_stars=min_github_stars))
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    return where, params


def _feed_item_sort_key(item: dict[str, Any]) -> tuple[str, str, float, str]:
    sort_at = str(item.get("published_at") or item.get("fetched_at") or item.get("created_at") or "")
    fetched_at = str(item.get("fetched_at") or "")
    try:
        score = float(item.get("relevance_score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return sort_at, fetched_at, score, str(item.get("id") or "")


def _merge_overlay_items(
    base_items: list[dict[str, Any]],
    overlay_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not overlay_items:
        return base_items
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    # Overlay rows may be a fresher copy of the same item already present in the
    # read-model card JSON. Keep the live row so fetched_at/order can advance.
    for item in [*overlay_items, *base_items]:
        item_id = str(item.get("id") or "")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        merged.append(item)
    merged.sort(key=_feed_item_sort_key, reverse=True)
    return merged[:limit]


def _private_manual_category_where(
    *,
    category: str,
    subcategory: str | None,
    manual_owner_user_id: str,
    search: str | None,
    min_github_stars: int,
) -> tuple[list[str], dict[str, Any]]:
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    if category == UNCATEGORIZED_SENTINEL:
        params["category"] = UNCATEGORIZED_SENTINEL
        where.append(f"{category_expr} = %(category)s")
    else:
        params["category_ids"] = expand_query_categories(category)
        where.append(f"{category_expr} = ANY(%(category_ids)s)")
    clean_subcategory = str(subcategory or "").strip()
    if clean_subcategory:
        params["subcategory_arr"] = json.dumps([clean_subcategory])
        where.append("i.ai_subcategories @> %(subcategory_arr)s::jsonb")
    return where, params


def _query_feed_by_category_private_manual_union_page(
    *,
    schema: str,
    category: str,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id:
        return None
    raw_category = (category or "").strip()
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    window_limit = safe_offset + safe_limit
    where, params = _private_manual_category_where(
        category=cache_category,
        subcategory=clean_subcategory,
        manual_owner_user_id=manual_owner_user_id,
        search=None,
        min_github_stars=min_github_stars,
    )
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            total_row = conn.execute(
                f"SELECT count(*) AS cnt FROM {schema}.items i {_where_sql(where)}",
                params,
            ).fetchone()
            private_total = int(dict(total_row).get("cnt") or 0) if total_row else 0
            if private_total <= 0:
                return None
            private_items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=window_limit,
                offset=0,
                status_user_id=user_id,
            )
    except Exception:
        return None

    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    base_cursor = (
        {**cursor_state, "rank_after": 0}
        if cursor_state
        else None
    )
    public_page = _query_feed_by_category_read_model(
        schema=schema,
        category=category,
        keyword=None,
        search=None,
        subcategory=clean_subcategory,
        offset=0,
        limit=window_limit,
        cursor=base_cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
        max_limit=window_limit,
    )
    if public_page is None:
        return None
    public_items = list(public_page.get("items") or [])
    merged = _merge_overlay_items(public_items, private_items, limit=window_limit)
    page_items = merged[safe_offset:safe_offset + safe_limit]
    total = int(public_page.get("total") or 0) + private_total
    next_offset = safe_offset + len(page_items)
    result = dict(public_page)
    result.update({
        "items": page_items,
        "category": category,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "next_cursor": None,
        "private_manual_overlay": True,
        "private_manual_overlay_page": True,
    })
    return result


def _query_private_manual_sections_overlay(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id or per_category is None:
        return None
    safe_limit = max(1, min(int(per_category), 200))
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    category_expr = _section_category_expr("i")
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            count_rows = conn.execute(
                f"""SELECT {category_expr} AS category, count(*) AS cnt
                      FROM {schema}.items i
                      {_where_sql(where)}
                     GROUP BY 1""",
                params,
            ).fetchall()
            status_join, status_params, status_alias = _item_status_join(schema, user_id)
            item_params = dict(params)
            item_params.update(status_params)
            item_params["limit"] = safe_limit
            item_rows = conn.execute(
                f"""WITH ranked AS (
                       SELECT {category_expr} AS section_category,
                              {_feed_cols(status_alias, include_heavy_json=False)},
                              row_number() OVER (
                                PARTITION BY {category_expr}
                                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                         i.fetched_at DESC NULLS LAST,
                                         i.relevance_score DESC NULLS LAST,
                                         i.id DESC
                              ) AS rn
                         FROM {schema}.items i
                         {status_join}
                         {_where_sql(where)}
                     )
                     SELECT *
                       FROM ranked
                      WHERE rn <= %(limit)s
                      ORDER BY section_category, rn""",
                item_params,
            ).fetchall()
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    for row in count_rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            cat_counts[category] = cnt
    if not cat_counts:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    for row in item_rows:
        raw = dict(row)
        category = raw.get("section_category") or UNCATEGORIZED_SENTINEL
        sections.setdefault(category, []).append(_normalize_item(raw))
    return {"sections": sections, "cat_counts": cat_counts}


def _merge_private_manual_sections_overlay(
    result: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    if not overlay:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    cat_counts = dict(result.get("cat_counts") or {})
    for category, count in (overlay.get("cat_counts") or {}).items():
        cat_counts[category] = int(cat_counts.get(category) or 0) + int(count or 0)
    for category, items in (overlay.get("sections") or {}).items():
        sections[category] = _merge_overlay_items(sections.get(category, []), list(items), limit=limit)
    out["sections"] = sections
    out["cat_counts"] = cat_counts
    out["total"] = sum(int(value or 0) for value in cat_counts.values())
    out["private_manual_overlay"] = True
    return out


def _query_private_manual_platforms_overlay(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not manual_owner_user_id or per_platform is None:
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    where, params = _private_manual_base_where(
        manual_owner_user_id=manual_owner_user_id,
        search=search,
        min_github_stars=min_github_stars,
    )
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 1000)
            total_row = conn.execute(
                f"SELECT count(*) AS cnt FROM {schema}.items i {_where_sql(where)}",
                params,
            ).fetchone()
            source_rows = conn.execute(
                f"""SELECT COALESCE(i.source, '') AS source, count(*) AS cnt
                      FROM {schema}.items i
                      {_where_sql(where)}
                     GROUP BY 1""",
                params,
            ).fetchall()
            category_rows = conn.execute(
                f"""SELECT cat.value AS category, count(DISTINCT i.id) AS cnt
                      FROM {schema}.items i
                      CROSS JOIN LATERAL jsonb_array_elements_text(i.ai_categories) AS cat(value)
                      {_where_sql(where + ["i.ai_categories IS NOT NULL"])}
                     GROUP BY cat.value""",
                params,
            ).fetchall()
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, "
                    "i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=0,
                status_user_id=user_id,
            )
    except Exception:
        return None

    total = int(dict(total_row).get("cnt") or 0) if total_row else 0
    if total <= 0:
        return None
    source_counts = {
        str(dict(row).get("source") or "user-submit"): int(dict(row).get("cnt") or 0)
        for row in source_rows
        if int(dict(row).get("cnt") or 0) > 0
    }
    category_counts = {
        str(dict(row).get("category") or UNCATEGORIZED_SENTINEL): int(dict(row).get("cnt") or 0)
        for row in category_rows
        if int(dict(row).get("cnt") or 0) > 0
    }
    return {
        "sections": {"manual": items},
        "platform_counts": {"manual": total},
        "source_counts": {"manual": source_counts},
        "category_counts": {"manual": category_counts},
    }


def _merge_private_manual_platforms_overlay(
    result: dict[str, Any],
    overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    if not overlay:
        return result
    limit = max(1, min(int(result.get("sample_limit") or 50), 200))
    out = dict(result)
    sections = {key: list(items) for key, items in (result.get("sections") or {}).items()}
    platform_counts = dict(result.get("platform_counts") or {})
    source_counts = {
        key: dict(value)
        for key, value in (result.get("source_counts") or {}).items()
    }
    category_counts = {
        key: dict(value)
        for key, value in (result.get("category_counts") or {}).items()
    }
    for platform, count in (overlay.get("platform_counts") or {}).items():
        platform_counts[platform] = int(platform_counts.get(platform) or 0) + int(count or 0)
    for platform, items in (overlay.get("sections") or {}).items():
        sections[platform] = _merge_overlay_items(sections.get(platform, []), list(items), limit=limit)
    for platform, counts in (overlay.get("source_counts") or {}).items():
        bucket = source_counts.setdefault(platform, {})
        for key, count in counts.items():
            bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
    for platform, counts in (overlay.get("category_counts") or {}).items():
        bucket = category_counts.setdefault(platform, {})
        for key, count in counts.items():
            bucket[key] = int(bucket.get(key) or 0) + int(count or 0)
    out["sections"] = sections
    out["platform_counts"] = platform_counts
    out["source_counts"] = source_counts
    out["category_counts"] = category_counts
    out["private_manual_overlay"] = True
    return out


def _info_read_model_active_version(conn: Any, schema: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""SELECT v.version_id, v.generated_at, v.max_fetched_at, v.meta_json
              FROM {schema}.info_read_model_state s
              JOIN {schema}.info_read_model_versions v
                ON v.version_id = s.active_version_id
             WHERE s.key = %(state_key)s
               AND v.status = 'complete'""",
        {"state_key": INFO_READ_MODEL_STATE_KEY},
    ).fetchone()
    return dict(row) if row else None


def _item_from_read_model_card(value: Any) -> dict[str, Any] | None:
    data = _json_value(value)
    if not isinstance(data, dict):
        return None
    return _normalize_item(data)


def _feed_result_cache_ttl(result: dict[str, Any] | None) -> int:
    if not isinstance(result, dict):
        return 0
    if result.get("degraded"):
        return 0
    if result.get("read_model_stale"):
        return 0
    return _feed_result_cache_ttl_sec()


def _feed_result_cacheable(result: dict[str, Any] | None) -> bool:
    return _feed_result_cache_ttl(result) > 0


def _item_info_categories(item: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for raw in _json_array(item.get("ai_categories")):
        category = canonicalize_category(raw)
        if category and category != "other" and category not in categories:
            categories.append(category)
    if categories:
        return categories
    single = canonicalize_category(item.get("ai_category"))
    if single and single != "other":
        return [single]
    return [UNCATEGORIZED_SENTINEL]


def _max_item_fetched_at(*groups: list[dict[str, Any]]) -> str | None:
    timestamps: list[str] = []
    for items in groups:
        timestamps.extend(str(item.get("fetched_at")) for item in items if item.get("fetched_at"))
    if not timestamps:
        return None
    return max(timestamps, key=sort_key)


def _query_feed_sections_search_read_model(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_category is None:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_category), 200))
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 8000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id,
                              ci.card_json,
                              ci.sort_at,
                              ci.fetched_at,
                              ci.relevance_score,
                              COALESCE(
                                NULLIF(NULLIF(ci.card_json #>> '{{ai_categories,0}}', ''), 'other'),
                                NULLIF(NULLIF(ci.card_json ->> 'ai_category', ''), 'other'),
                                %(uncategorized)s
                              ) AS category
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     counts AS (
                       SELECT category,
                              count(item_id)::integer AS total_count
                         FROM matched_cards
                        GROUP BY category
                     ),
                     page_rows AS (
                       SELECT category,
                              total_count,
                              page_rank AS rn,
                              sort_at,
                              fetched_at,
                              relevance_score,
                              item_id,
                              card_json
                         FROM (
                           SELECT mc.*,
                                  c.total_count,
                                  row_number() OVER (
                                    PARTITION BY mc.category
                                    ORDER BY {_info_scope_item_order_sql("mc")}
                                  ) AS page_rank
                             FROM matched_cards mc
                             JOIN counts c
                               ON c.category = mc.category
                         ) ranked
                        WHERE page_rank <= %(limit)s
                     )
                     SELECT pr.category, pr.total_count, pr.rn, pr.card_json
                       FROM page_rows pr
                      ORDER BY pr.category,
                               pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC""",
                {
                    "version_id": str(active.get("version_id") or ""),
                    "limit": safe_limit,
                    "search_like": f"%{search}%",
                    "uncategorized": UNCATEGORIZED_SENTINEL,
                },
            ).fetchall()
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        total = int(data.get("total_count") or 0)
        if total > 0:
            cat_counts[category] = total
        item = _item_from_read_model_card(data.get("card_json"))
        if item:
            sections.setdefault(category, []).append(item)
            all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    section_next_cursors = {
        category: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform="_all", dimension="section_category", value=category),
            rank_after=len(sections.get(category, [])),
            total_count=int(cat_counts.get(category) or 0),
        )
        for category in cat_counts
    }
    result = {
        "sections": sections,
        "total": sum(cat_counts.values()),
        "cat_counts": cat_counts,
        "personalized": False,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_search_v1",
        "read_model_version_id": version_id_str,
        "section_next_cursors": section_next_cursors,
    }
    overlay = _query_private_manual_sections_overlay(
        schema=schema,
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_sections_overlay(result, overlay)


def _query_feed_platforms_search_read_model(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_platform is None:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 8000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            common_params = {
                "version_id": str(active.get("version_id") or ""),
                "search_like": f"%{search}%",
            }
            card_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id,
                              ci.card_json,
                              ci.platform,
                              ci.source,
                              ci.sort_at,
                              ci.fetched_at,
                              ci.relevance_score
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     counts AS (
                       SELECT platform,
                              count(item_id)::integer AS total_count
                         FROM matched_cards
                        GROUP BY platform
                     ),
                     page_rows AS (
                       SELECT platform,
                              total_count,
                              page_rank AS rn,
                              sort_at,
                              fetched_at,
                              relevance_score,
                              item_id,
                              card_json
                         FROM (
                           SELECT mc.*,
                                  c.total_count,
                                  row_number() OVER (
                                    PARTITION BY mc.platform
                                    ORDER BY {_info_scope_item_order_sql("mc")}
                                  ) AS page_rank
                             FROM matched_cards mc
                             JOIN counts c
                               ON c.platform = mc.platform
                         ) ranked
                        WHERE page_rank <= %(limit)s
                     )
                     SELECT pr.platform, pr.total_count, pr.rn, pr.card_json
                       FROM page_rows pr
                      ORDER BY pr.platform,
                               pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC""",
                {**common_params, "limit": safe_limit},
            ).fetchall()
            source_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id, ci.platform, ci.source
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     )
                    SELECT platform, source, count(DISTINCT item_id)::integer AS cnt
                      FROM matched_cards
                     WHERE COALESCE(source, '') != ''
                     GROUP BY platform, source
                     ORDER BY platform, cnt DESC""",
                common_params,
            ).fetchall()
            category_rows = conn.execute(
                f"""WITH matched_cards AS MATERIALIZED (
                       SELECT ci.item_id, ci.platform, ci.card_json
                         FROM {schema}.info_card_items ci
                        WHERE ci.version_id = %(version_id)s::uuid
                          AND {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                          AND {_info_display_source_filter("ci")}
                     ),
                     matched_categories AS (
                       SELECT mc.platform,
                              cat.value AS category,
                              mc.item_id
                         FROM matched_cards mc
                        CROSS JOIN LATERAL jsonb_array_elements_text(
                              CASE
                                WHEN jsonb_typeof(mc.card_json -> 'ai_categories') = 'array'
                                THEN mc.card_json -> 'ai_categories'
                                ELSE '[]'::jsonb
                              END
                            ) AS cat(value)
                       UNION ALL
                       SELECT mc.platform,
                              COALESCE(
                                NULLIF(NULLIF(mc.card_json ->> 'ai_category', ''), 'other'),
                                %(uncategorized)s
                              ) AS category,
                              mc.item_id
                         FROM matched_cards mc
                        WHERE jsonb_array_length(
                              CASE
                                WHEN jsonb_typeof(mc.card_json -> 'ai_categories') = 'array'
                                THEN mc.card_json -> 'ai_categories'
                                ELSE '[]'::jsonb
                              END
                            ) = 0
                     )
                    SELECT platform, category, count(DISTINCT item_id)::integer AS cnt
                      FROM matched_categories
                     WHERE COALESCE(category, '') != ''
                     GROUP BY platform, category
                     ORDER BY platform, cnt DESC""",
                {**common_params, "uncategorized": UNCATEGORIZED_SENTINEL},
            ).fetchall()
    except Exception:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    platform_counts: dict[str, int] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        total = int(data.get("total_count") or 0)
        if total > 0:
            platform_counts[platform] = total
        item = _item_from_read_model_card(data.get("card_json"))
        if item:
            sections.setdefault(platform, []).append(item)
            all_section_items.append(item)
    source_counts: dict[str, dict[str, int]] = {}
    for row in source_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        source = data.get("source") or ""
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            source_counts.setdefault(platform, {})[source] = cnt
    category_counts: dict[str, dict[str, int]] = {}
    for row in category_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        cnt = int(data.get("cnt") or 0)
        if cnt > 0:
            category_counts.setdefault(platform, {})[category] = cnt
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    platform_next_cursors = {
        platform: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform=platform, dimension="all", value=""),
            rank_after=len(sections.get(platform, [])),
            total_count=int(platform_counts.get(platform) or 0),
        )
        for platform in platform_counts
    }
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_search_v1",
        "read_model_version_id": version_id_str,
        "platform_next_cursors": platform_next_cursors,
    }
    overlay = _query_private_manual_platforms_overlay(
        schema=schema,
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_platforms_overlay(result, overlay)


def _query_feed_platforms_read_model(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_platform is None:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_platform), 200))
    try:
        with connect() as conn:
            if not _set_info_read_model_timeouts(conn):
                return None
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            version_id = active["version_id"]
            scope_rows = conn.execute(
                f"""SELECT sc.platform, sc.dimension, sc.value,
                           sc.total_count,
                           sc.max_sort_at
                      FROM {schema}.info_scopes sc
                     WHERE sc.version_id = %(version_id)s
                       AND sc.dimension IN ('all', 'source', 'category')
                     ORDER BY sc.platform, sc.dimension, sc.total_count DESC""",
                {"version_id": version_id},
            ).fetchall()
            card_rows = conn.execute(
                f"""WITH all_scope_items AS MATERIALIZED (
                       SELECT sc.platform, page.rank, page.item_id,
                              page.sort_at, page.fetched_at, page.relevance_score
                         FROM (
                               SELECT platform, scope_key
                                 FROM {schema}.info_scopes
                                WHERE version_id = %(version_id)s
                                  AND dimension = 'all'
                              ) sc
                         CROSS JOIN LATERAL (
                               SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                                 FROM {schema}.info_scope_items si
                                 JOIN {schema}.info_card_items ci
                                   ON ci.version_id = si.version_id
                                  AND ci.item_id = si.item_id
                                WHERE si.version_id = %(version_id)s
                                  AND si.scope_key = sc.scope_key
                                  AND {_info_display_source_filter("ci")}
                                ORDER BY {_info_scope_item_order_sql("si")}
                                LIMIT %(limit)s
                              ) page
                     )
                     SELECT page.platform, page.rank, page.sort_at,
                            page.fetched_at, page.relevance_score, page.item_id,
                            card.card_json
                       FROM all_scope_items page
                       CROSS JOIN LATERAL (
                             SELECT ci.card_json
                               FROM {schema}.info_card_items ci
                              WHERE ci.version_id = %(version_id)s
                                AND ci.item_id = page.item_id
                              OFFSET 0
                       ) card
                      ORDER BY page.platform,
                               page.sort_at DESC NULLS LAST,
                               page.fetched_at DESC NULLS LAST,
                               page.relevance_score DESC NULLS LAST,
                               page.item_id DESC""",
                {"version_id": version_id, "limit": safe_limit},
            ).fetchall()
            _commit_safely(conn)
    except Exception:
        return None

    # perf-v27 P4: platform/source/category 维度已不再物化——新版本读模型里
    # 这些 scope 不存在,必须显式返回 None 让调用方落到 live 现场查,
    # 否则会把"维度已下线"渲染成空平台视图。
    if not scope_rows:
        return None

    platform_counts: dict[str, int] = {}
    source_counts: dict[str, dict[str, int]] = {}
    category_counts: dict[str, dict[str, int]] = {}
    bookmark_counts: dict[str, int] = {}
    for row in scope_rows:
        data = dict(row)
        if (
            (data.get("platform") or "") == "twitter"
            and (data.get("dimension") or "") == "source"
            and (data.get("value") or "") == "bookmarks"
        ):
            bookmark_counts["twitter"] = int(data.get("total_count") or 0)
    for row in scope_rows:
        data = dict(row)
        platform = data.get("platform") or "_unknown"
        dimension = data.get("dimension") or ""
        value = data.get("value") or ""
        total = int(data.get("total_count") or 0)
        if total <= 0:
            continue
        if dimension == "all":
            platform_counts[platform] = max(0, total - int(bookmark_counts.get(platform) or 0))
        elif dimension == "source" and value:
            if platform == "twitter" and value == "bookmarks":
                continue
            source_counts.setdefault(platform, {})[value] = total
        elif dimension == "category" and value:
            category_counts.setdefault(platform, {})[value] = total

    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        item = _item_from_read_model_card(data.get("card_json"))
        if not item:
            continue
        platform = data.get("platform") or item.get("platform") or "_unknown"
        sections.setdefault(platform, []).append(item)
        all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    platform_next_cursors = {
        platform: _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=_info_scope_key(platform=platform, dimension="all", value=""),
            rank_after=len(sections.get(platform, [])),
            total_count=int(platform_counts.get(platform) or 0),
        )
        for platform in platform_counts
    }
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": _timestamp_value(active.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items),
        "sample_limit": safe_limit,
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id_str,
        "platform_next_cursors": platform_next_cursors,
    }
    overlay = _query_private_manual_platforms_overlay(
        schema=schema,
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_platforms_overlay(result, overlay)


def _query_feed_sections_read_model(
    *,
    schema: str,
    per_category: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if per_category is None:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    safe_limit = max(1, min(int(per_category), 200))
    try:
        with connect() as conn:
            if not _set_info_read_model_timeouts(conn):
                return None
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return None
            version_id = active["version_id"]
            count_rows = conn.execute(
                f"""SELECT sc.value AS category,
                           sum(sc.total_count)::integer AS total_count,
                           max(sc.max_sort_at) AS max_sort_at
                      FROM {schema}.info_scopes sc
                     WHERE sc.version_id = %(version_id)s
                       AND sc.dimension = 'section_category'
                       AND sc.value != ''
                     GROUP BY sc.value
                     ORDER BY total_count DESC""",
                {"version_id": version_id},
            ).fetchall()
            card_rows = conn.execute(
                f"""WITH scope_rows AS (
                       SELECT sc.value AS category,
                              sc.scope_key
                         FROM {schema}.info_scopes sc
                        WHERE sc.version_id = %(version_id)s
                          AND sc.dimension = 'section_category'
                          AND sc.value != ''
                     )
                     SELECT sr.category,
                            page.rank,
                            page.fetched_at,
                            page.sort_at,
                            page.relevance_score,
                            page.item_id,
                            ci.card_json
                       FROM scope_rows sr
                      CROSS JOIN LATERAL (
                            SELECT si.rank, si.sort_at, si.fetched_at, si.relevance_score, si.item_id
                              FROM {schema}.info_scope_items si
                              JOIN {schema}.info_card_items ci
                                ON ci.version_id = si.version_id
                               AND ci.item_id = si.item_id
                             WHERE si.version_id = %(version_id)s
                               AND si.scope_key = sr.scope_key
                               AND {_info_display_source_filter("ci")}
                             ORDER BY {_info_scope_item_order_sql("si")}
                             LIMIT %(limit)s
                           ) AS page
                       JOIN {schema}.info_card_items ci
                         ON ci.version_id = %(version_id)s
                        AND ci.item_id = page.item_id
                      ORDER BY sr.category,
                               page.sort_at DESC NULLS LAST,
                               page.fetched_at DESC NULLS LAST,
                               page.relevance_score DESC NULLS LAST,
                               page.item_id DESC""",
                {"version_id": version_id, "limit": safe_limit},
            ).fetchall()
            # perf-v27 P4: 表头计数用全量快照覆盖(90 天保留期总量,post-fetch
            # 重算)——scopes.total_count 现在只反映 7 天热窗口。
            snapshot_counts = _info_pill_counts_overlay(conn, schema, "section_category")
            _commit_safely(conn)
    except Exception:
        return None

    cat_counts: dict[str, int] = {}
    for row in count_rows:
        data = dict(row)
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        total = int(data.get("total_count") or 0)
        snapshot_total = int(snapshot_counts.get(category) or 0)
        total = max(total, snapshot_total)
        if total > 0:
            cat_counts[category] = total
    if not cat_counts:
        return None

    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in card_rows:
        data = dict(row)
        item = _item_from_read_model_card(data.get("card_json"))
        if not item:
            continue
        category = data.get("category") or UNCATEGORIZED_SENTINEL
        sections.setdefault(category, []).append(item)
        all_section_items.append(item)
    if user_id:
        sections = _apply_user_status_overlay_to_sections(
            schema=schema,
            sections=sections,
            user_id=user_id,
        )
        all_section_items = [item for items in sections.values() for item in items]

    overview_generated_at = _timestamp_value(active.get("generated_at"))
    overview_max_fetched_at = _timestamp_value(active.get("max_fetched_at")) or _max_fetched_at(all_section_items)
    version_id_str = str(active.get("version_id")) if active.get("version_id") else None
    section_next_cursors: dict[str, dict[str, Any] | None] = {}
    for category, items in sections.items():
        total = int(cat_counts.get(category) or 0)
        next_offset = len(items)
        scope_key = _info_scope_key(platform="_all", dimension="section_category", value=category)
        next_cursor = _info_read_model_next_cursor(
            version_id=version_id_str,
            scope_key=scope_key,
            rank_after=next_offset,
            total_count=total,
        )
        section_next_cursors[category] = next_cursor
        page = {
            "items": items,
            "category": category,
            "total": total,
            "offset": 0,
            "limit": safe_limit,
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "data_backend": feed_read_backend(),
            "read_model": "info_platforms_v1",
            "read_model_version_id": version_id_str,
            "scope_key": scope_key,
            "scope_dimension": "section_category",
            "scope_value": category,
            "overview_generated_at": overview_generated_at,
            "overview_max_fetched_at": overview_max_fetched_at,
        }
        if next_cursor:
            page["next_cursor"] = next_cursor
        if not user_id:
            _cache_set_copy(
                _info_read_model_section_category_page_cache_key(
                    schema=schema,
                    category=category,
                    offset=0,
                    limit=safe_limit,
                ),
                page,
            )

    result = {
        "sections": sections,
        "total": sum(cat_counts.values()),
        "cat_counts": cat_counts,
        "personalized": False,
        "data_backend": feed_read_backend(),
        "overview_generated_at": overview_generated_at,
        "overview_max_fetched_at": overview_max_fetched_at,
        "sample_limit": safe_limit,
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id_str,
        "section_next_cursors": section_next_cursors,
    }
    overlay = _query_private_manual_sections_overlay(
        schema=schema,
        per_category=per_category,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    return _merge_private_manual_sections_overlay(result, overlay)


def _query_feed_by_category_read_model(
    *,
    schema: str,
    category: str,
    keyword: str | None,
    search: str | None,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    max_limit: int = 200,
) -> dict[str, Any] | None:
    if keyword:
        return None
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    raw_category = (category or "").strip()
    if not raw_category:
        return None
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), max(1, int(max_limit or 200))))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    cursor_exclude_ids = list(cursor_state.get("exclude_ids") or []) if cursor_state else []
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    # perf-v27 P4: 读模型只物化每 scope 首屏 TOP_N(7 天热窗口)——它只服务
    # 第一页;任何续页(offset>0 或带 cursor)一律返回 None,落到 live 现场
    # keyset 查询(覆盖 90 天全保留期,含滑进冷数据)。若在这里继续用被
    # 封顶的 total 判 has_more,模块流会在 7 天/50 条处假性到底。
    if effective_offset > 0 or cursor_state:
        return None
    page_cache_key = _info_read_model_section_category_page_cache_key(
        schema=schema,
        category=cache_category,
        subcategory=clean_subcategory,
        offset=effective_offset,
        limit=safe_limit,
        version_id=cursor_version_id,
    )
    cached_page = None if cursor_exclude_ids else _cache_get_copy(page_cache_key)
    if cached_page is not None:
        if user_id:
            cached_page = dict(cached_page)
            cached_page["items"] = _apply_user_status_overlay(
                schema=schema,
                items=list(cached_page.get("items") or []),
                user_id=user_id,
            )
        return cached_page
    exclude_predicate_sql = "NOT (si.item_id = ANY(%(exclude_ids)s))" if cursor_exclude_ids else "TRUE"
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _feed_more_timeout_ms())
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_rows AS (
                       SELECT sc.version_id, sc.scope_key, sc.total_count
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                          AND sc.dimension = %(scope_dimension)s
                     ),
                     summary AS (
                       SELECT count(*)::integer AS scope_count,
                              max(total_count)::integer AS total_count
                         FROM scope_rows
                     ),
                     page_rows AS (
                       SELECT si.rank, si.fetched_at, si.relevance_score, si.item_id,
                              si.sort_at, ci.card_json
                         FROM scope_rows sr
                         JOIN {schema}.info_scope_items si
                           ON si.version_id = sr.version_id
                          AND si.scope_key = sr.scope_key
                         JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                          AND ci.item_id = si.item_id
                        WHERE {exclude_predicate_sql}
                          AND {_info_display_source_filter("ci")}
                        ORDER BY {_info_scope_item_order_sql("si")}
                        LIMIT %(limit)s OFFSET %(offset)s
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.fetched_at, pr.relevance_score, pr.item_id,
                            pr.sort_at, pr.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "scope_dimension": scope_dimension,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "end_rank": effective_offset + safe_limit,
                    "exclude_ids": cursor_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    if not items:
        # perf-v27 P4: 模型有 scope 但首页无可渲染行(如展示源过滤后为空)——
        # 交给 live,别把空首页当"该模块没内容"。
        return None
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    # perf-v27 P4: has_more 恒 True + 不下发读模型 cursor——模型只知道 7 天
    # 热窗口的 total,判不了"90 天里还有没有更老的"。续页由前端带 offset 发起,
    # 本函数对 offset>0 返回 None → live keyset 接管直到真正到底。
    result = {
        "items": items,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": True,
        "next_offset": next_offset,
        "data_backend": feed_read_backend(),
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": scope_dimension,
        "scope_value": scope_value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if user_id:
        return result
    if cursor_exclude_ids:
        return result
    return _cache_set_copy(page_cache_key, result)


def _query_feed_by_category_search_read_model(
    *,
    schema: str,
    category: str,
    keyword: str | None,
    search: str | None,
    subcategory: str | None,
    offset: int,
    limit: int,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if keyword:
        return None
    if manual_owner_user_id:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    raw_category = (category or "").strip()
    if not raw_category:
        return None
    cache_category = canonicalize_category(raw_category) or raw_category
    if not cache_category:
        return None
    clean_subcategory = str(subcategory or "").strip() or None
    scope_dimension = "section_subcategory" if clean_subcategory else "section_category"
    scope_value = (
        _info_section_subcategory_value(cache_category, clean_subcategory)
        if clean_subcategory
        else cache_category
    )
    scope_key = _info_scope_key(platform="_all", dimension=scope_dimension, value=scope_value)
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_rows AS (
                       SELECT sc.version_id, sc.scope_key
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                          AND sc.dimension = %(scope_dimension)s
                     ),
                     summary AS (
                       SELECT (SELECT count(*)::integer FROM scope_rows) AS scope_count,
                              (
                                SELECT count(*)::integer
                                  FROM scope_rows sr
                                  JOIN {schema}.info_scope_items si
                                    ON si.version_id = sr.version_id
                                   AND si.scope_key = sr.scope_key
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                                   AND {_info_display_source_filter("ci")}
                              ) AS total_count
                     ),
                     page_rows AS (
                       SELECT rank, item_id, sort_at, fetched_at, relevance_score
                         FROM (
                           SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                             FROM scope_rows sr
                             JOIN {schema}.info_scope_items si
                               ON si.version_id = sr.version_id
                              AND si.scope_key = sr.scope_key
                             JOIN {schema}.info_card_items ci
                               ON ci.version_id = si.version_id
                              AND ci.item_id = si.item_id
                            WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                              AND {_info_display_source_filter("ci")}
                            ORDER BY {_info_scope_item_order_sql("si")}
                            LIMIT %(limit)s OFFSET %(offset)s
                         ) AS page_match
                        ORDER BY sort_at DESC NULLS LAST,
                                 fetched_at DESC NULLS LAST,
                                 relevance_score DESC NULLS LAST,
                                 item_id DESC
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, page_ci.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                       LEFT JOIN {schema}.info_card_items page_ci
                         ON page_ci.version_id = av.version_id
                        AND page_ci.item_id = pr.item_id
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "scope_dimension": scope_dimension,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "cursor_version_id": cursor_version_id or "",
                    "search_like": f"%{search}%",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_search_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": scope_dimension,
        "scope_value": scope_value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
    return result


def _query_feed_by_platform_read_model(
    *,
    schema: str,
    platform: str,
    offset: int,
    limit: int,
    source: str | None,
    group: str | None,
    category: str | None,
    search: str | None,
    exclude_ids: list[str] | None,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if not _can_use_info_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    ):
        return None
    scope = _info_platform_scope(platform=platform, source=source, group=group, category=category)
    if not scope:
        return None
    dimension, value, scope_key = scope
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    cursor_exclude_ids = list(cursor_state.get("exclude_ids") or []) if cursor_state else []
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    effective_offset = (
        int(cursor_state["rank_after"])
        if cursor_state
        else max(safe_offset, len(clean_exclude_ids)) if clean_exclude_ids else safe_offset
    )
    # Keep the normal read-model page cache hot. Only live-overlay cursors need a
    # SQL anti-filter because overlay rows can be fresher copies of later ranks.
    effective_exclude_ids: list[str] = cursor_exclude_ids
    page_cache_key = _info_read_model_page_cache_key(
        schema=schema,
        platform=platform,
        source=source,
        group=group,
        category=category,
        offset=effective_offset,
        limit=safe_limit,
        exclude_ids=effective_exclude_ids,
        version_id=cursor_version_id,
    )
    cached_page = _cache_get_copy(page_cache_key)
    if cached_page is not None:
        if user_id:
            cached_page = dict(cached_page)
            cached_page["items"] = _apply_user_status_overlay(
                schema=schema,
                items=list(cached_page.get("items") or []),
                user_id=user_id,
            )
        return cached_page
    exclude_sql = "AND NOT (si.item_id = ANY(%(exclude_ids)s))" if effective_exclude_ids else ""
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _feed_more_timeout_ms())
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_row AS (
                       SELECT sc.total_count
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                     ),
                     page_rows AS (
                       SELECT si.rank, si.item_id, si.sort_at, si.fetched_at,
                              si.relevance_score, ci.card_json
                         FROM {schema}.info_scope_items si
                         JOIN active_version av
                           ON av.version_id = si.version_id
                         JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                         AND ci.item_id = si.item_id
                        WHERE si.scope_key = %(scope_key)s
                          {exclude_sql}
                          AND {_info_display_source_filter("ci")}
                        ORDER BY {_info_scope_item_order_sql("si")}
                        LIMIT %(limit)s OFFSET %(offset)s
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at, sr.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, pr.card_json
                       FROM active_version av
                       JOIN scope_row sr ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "exclude_ids": effective_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_platforms_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": dimension,
        "scope_value": value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
        if effective_exclude_ids:
            result["next_cursor"]["exclude_ids"] = effective_exclude_ids
    if user_id:
        return result
    return _cache_set_copy(page_cache_key, result)


def _query_feed_by_platform_search_read_model(
    *,
    schema: str,
    platform: str,
    offset: int,
    limit: int,
    source: str | None,
    group: str | None,
    category: str | None,
    search: str | None,
    exclude_ids: list[str] | None,
    cursor: dict[str, Any] | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
) -> dict[str, Any] | None:
    if platform == "manual" and manual_owner_user_id:
        return None
    if not _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return None
    scope = _info_platform_scope(platform=platform, source=source, group=group, category=category)
    if not scope:
        return None
    dimension, value, scope_key = scope
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    cursor_state = _normalize_info_read_model_cursor(cursor, expected_scope_key=scope_key)
    cursor_version_id = str(cursor_state["version_id"]) if cursor_state else None
    effective_offset = int(cursor_state["rank_after"]) if cursor_state else safe_offset
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    exclude_sql = "AND NOT (si.item_id = ANY(%(exclude_ids)s))" if clean_exclude_ids else ""
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            rows = conn.execute(
                f"""WITH active_version AS (
                       SELECT v.version_id, v.generated_at, v.max_fetched_at
                         FROM {schema}.info_read_model_versions v
                        WHERE v.status = 'complete'
                          AND (
                            (%(cursor_version_id)s != '' AND v.version_id::text = %(cursor_version_id)s)
                            OR (
                              %(cursor_version_id)s = ''
                              AND v.version_id = (
                                SELECT s.active_version_id
                                  FROM {schema}.info_read_model_state s
                                 WHERE s.key = %(state_key)s
                              )
                            )
                          )
                        LIMIT 1
                     ),
                     scope_row AS (
                       SELECT sc.version_id, sc.scope_key
                         FROM {schema}.info_scopes sc
                         JOIN active_version av
                           ON av.version_id = sc.version_id
                        WHERE sc.scope_key = %(scope_key)s
                     ),
                     summary AS (
                       SELECT (SELECT count(*)::integer FROM scope_row) AS scope_count,
                              (
                                SELECT count(*)::integer
                                  FROM scope_row sr
                                  JOIN {schema}.info_scope_items si
                                    ON si.version_id = sr.version_id
                                   AND si.scope_key = sr.scope_key
                                  JOIN {schema}.info_card_items ci
                                    ON ci.version_id = si.version_id
                                   AND ci.item_id = si.item_id
                                 WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                                   AND {_info_display_source_filter("ci")}
                              ) AS total_count
                     ),
                     page_rows AS (
                       SELECT rank, item_id, sort_at, fetched_at, relevance_score
                         FROM (
                           SELECT si.rank, si.item_id, si.sort_at, si.fetched_at, si.relevance_score
                             FROM scope_row sr
                             JOIN {schema}.info_scope_items si
                               ON si.version_id = sr.version_id
                              AND si.scope_key = sr.scope_key
                             JOIN {schema}.info_card_items ci
                               ON ci.version_id = si.version_id
                              AND ci.item_id = si.item_id
                            WHERE {_read_model_card_search_sql(search_text_expr="ci.search_text")}
                              AND {_info_display_source_filter("ci")}
                              {exclude_sql}
                            ORDER BY {_info_scope_item_order_sql("si")}
                            LIMIT %(limit)s OFFSET %(offset)s
                         ) AS page_match
                        ORDER BY sort_at DESC NULLS LAST,
                                 fetched_at DESC NULLS LAST,
                                 relevance_score DESC NULLS LAST,
                                 item_id DESC
                     )
                     SELECT av.version_id, av.generated_at, av.max_fetched_at,
                            summary.scope_count, summary.total_count,
                            pr.rank, pr.item_id, pr.sort_at, pr.fetched_at,
                            pr.relevance_score, page_ci.card_json
                       FROM active_version av
                       JOIN summary ON TRUE
                       LEFT JOIN page_rows pr ON TRUE
                       LEFT JOIN {schema}.info_card_items page_ci
                         ON page_ci.version_id = av.version_id
                        AND page_ci.item_id = pr.item_id
                      ORDER BY pr.sort_at DESC NULLS LAST,
                               pr.fetched_at DESC NULLS LAST,
                               pr.relevance_score DESC NULLS LAST,
                               pr.item_id DESC NULLS LAST""",
                {
                    "state_key": INFO_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                    "limit": safe_limit,
                    "offset": effective_offset,
                    "exclude_ids": clean_exclude_ids,
                    "cursor_version_id": cursor_version_id or "",
                    "search_like": f"%{search}%",
                },
            ).fetchall()
    except Exception:
        return None

    if not rows:
        return None
    first_row = dict(rows[0])
    if int(first_row.get("scope_count") or 0) <= 0:
        return None
    total = int(first_row.get("total_count") or 0)
    items = [
        item
        for item in (_item_from_read_model_card(dict(row).get("card_json")) for row in rows)
        if item
    ]
    items = _apply_user_status_overlay(schema=schema, items=items, user_id=user_id)
    next_offset = effective_offset + len(items)
    version_id = str(first_row.get("version_id")) if first_row.get("version_id") else None
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": effective_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
        "read_model": "info_search_v1",
        "read_model_version_id": version_id,
        "scope_key": scope_key,
        "scope_dimension": dimension,
        "scope_value": value,
        "overview_generated_at": _timestamp_value(first_row.get("generated_at")),
        "overview_max_fetched_at": _timestamp_value(first_row.get("max_fetched_at")),
    }
    if next_offset < total:
        result["next_cursor"] = {
            "version_id": version_id,
            "scope_key": scope_key,
            "rank_after": next_offset,
        }
    return result


def query_feed_platforms(
    *,
    per_platform: int | None = 50,
    search: str | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    """v18.0 nav-merge: 信息 tab 复用本函数；强制 AI 相关性过滤（D3）。

    sections 是首屏样本；platform_counts/source_counts/category_counts 是全量
    聚合口径，不能再从首屏样本推导。
    """
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.visible = 1")
    # v18.0 PRD §Spec-2: 强制 AI 相关性过滤（OR 双字段口径）
    _add_ai_relevance_filter(where)
    _add_search_filter(where, params, search)
    # BF-0515-singleflight: cache full /api/feed/platforms response (no result
    # cache existed before — every call ran 5 SQLs). Singleflight prevents N
    # cold-start callers from each running the same SQLs.
    result_cache_ttl = _feed_result_cache_ttl_sec()
    result_cache_key = (
        "feed_platforms_result",
        schema,
        per_platform,
        search or "",
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
    )
    cached_result = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
    if cached_result is not None:
        return cached_result
    if search:
        read_model_result = _query_feed_platforms_search_read_model(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
    else:
        read_model_result = _query_feed_platforms_read_model(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
    if read_model_result is not None:
        cache_ttl = _feed_result_cache_ttl(read_model_result)
        if cache_ttl > 0:
            return _cache_set_copy_with_ttl(result_cache_key, read_model_result, cache_ttl)
        return read_model_result
    if search and _can_use_info_search_read_model(
        search=search,
        user_id=user_id,
        public_only=public_only,
        min_github_stars=min_github_stars,
    ):
        return _degraded_feed_platforms_result("info_search_read_model_unavailable")
    use_platforms_fast_path = _can_use_platforms_mv_fast_path(
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    )
    if _remote_feed_live_circuit_open():
        if use_platforms_fast_path:
            fallback = _read_local_read_cache(
                _feed_platforms_local_cache_name(
                    per_platform=per_platform,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
        return _degraded_feed_platforms_result()

    def _compute() -> dict:
        cached_inside = _cache_get_copy_with_ttl(result_cache_key, result_cache_ttl)
        if cached_inside is not None:
            return cached_inside
        return _query_feed_platforms_uncached(
            schema=schema,
            per_platform=per_platform,
            search=search,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
            where=where,
            params=params,
            result_cache_key=result_cache_key,
        )

    try:
        return _singleflight_sync(result_cache_key, _compute)
    except RemoteDBError:
        _mark_remote_feed_live_circuit_open()
        if use_platforms_fast_path:
            fallback = _read_local_read_cache(
                _feed_platforms_local_cache_name(
                    per_platform=per_platform,
                    public_only=public_only,
                    min_github_stars=min_github_stars,
                )
            )
            if fallback is not None:
                return _cache_set_copy(result_cache_key, fallback)
            return _degraded_feed_platforms_result()
        return _degraded_feed_platforms_result()


def _query_feed_platforms_uncached(
    *,
    schema: str,
    per_platform: int | None,
    search: str | None,
    user_id: str | None,
    public_only: bool,
    manual_owner_user_id: str | None,
    min_github_stars: int,
    where: list[str],
    params: dict[str, Any],
    result_cache_key: tuple[Any, ...],
) -> dict:
    with connect() as conn:
        item_params = dict(params)
        item_limit_sql = ""
        if per_platform is not None:
            item_params["per_platform"] = per_platform
            item_limit_sql = "WHERE rn <= %(per_platform)s"
        _set_short_statement_timeout(conn, _remote_feed_live_timeout_ms())
        try:
            status_join, status_params, status_alias = _item_status_join(schema, user_id)
            item_params.update(status_params)
            rows = conn.execute(
                f"""WITH ranked AS (
                       SELECT {_feed_cols(status_alias, include_heavy_json=False)},
                              row_number() OVER (
                                PARTITION BY i.platform
                                ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                                         i.fetched_at DESC NULLS LAST,
                                         i.relevance_score DESC NULLS LAST,
                                         i.id DESC
                              ) AS rn
                         FROM {schema}.items i
                         {status_join}
                         {_where_sql(where)}
                     )
                     SELECT * FROM ranked
                     {item_limit_sql}
                     ORDER BY platform,
                              COALESCE(published_at, fetched_at) DESC NULLS LAST,
                              fetched_at DESC NULLS LAST,
                              relevance_score DESC NULLS LAST,
                              id DESC""",
                item_params,
            ).fetchall()
            platform_counts, source_counts, category_counts = _platform_overview_counts_from_items(
                conn,
                schema,
                where,
                params,
            )
            _warm_platform_page_count_cache(
                schema=schema,
                platform_counts=platform_counts,
                source_counts=source_counts,
                category_counts=category_counts,
                search=search,
                public_only=public_only,
                manual_owner_user_id=manual_owner_user_id,
                user_id=user_id,
                min_github_stars=min_github_stars,
            )
        except Exception:
            _rollback_safely(conn)
            raise RemoteDBError("platforms live path failed")
    sections: dict[str, list[dict[str, Any]]] = {}
    all_section_items: list[dict[str, Any]] = []
    for row in rows:
        item = _normalize_item(dict(row))
        sections.setdefault(item.get("platform") or "_unknown", []).append(item)
        all_section_items.append(item)
    result = {
        "sections": sections,
        "platform_counts": platform_counts,
        "source_counts": source_counts,
        "category_counts": category_counts,
        "data_backend": feed_read_backend(),
        "overview_generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overview_max_fetched_at": _max_fetched_at(all_section_items),
        "sample_limit": int(per_platform) if per_platform is not None else None,
    }
    if _can_use_platforms_mv_fast_path(
        per_platform=per_platform,
        search=search,
        user_id=user_id,
        manual_owner_user_id=manual_owner_user_id,
    ):
        _write_local_read_cache_async(
            _feed_platforms_local_cache_name(
                per_platform=per_platform,
                public_only=public_only,
                min_github_stars=min_github_stars,
            ),
            result,
        )
    return _cache_set_copy(result_cache_key, result)


def query_feed_by_platform(
    *,
    platform: str,
    offset: int = 0,
    limit: int = 50,
    source: str | None = None,
    group: str | None = None,
    category: str | None = None,
    search: str | None = None,
    exclude_ids: list[str] | None = None,
    cursor: dict[str, Any] | None = None,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.platform = %(platform)s")
    params["platform"] = platform
    _add_search_filter(where, params, search)
    where.append("i.visible = 1")
    # 2026-05-22 read model: /platforms/more must page through the same
    # AI-relevant item universe used by /platforms counts and first-page cards.
    _add_ai_relevance_filter(where)
    if source:
        where.append("i.source = %(source)s")
        params["source"] = source
    if group:
        if group == "未分组":
            where.append(
                "((i.detail_json ->> 'group') IN ('未分组','独立频道') OR (i.detail_json ->> 'group') IS NULL)"
            )
        else:
            where.append("(i.detail_json ->> 'group') = %(group)s")
            params["group"] = group
    if category and category != UNCATEGORIZED_SENTINEL:
        where.append("i.ai_categories IS NOT NULL")
    _add_category_filter(where, params, category)
    count_where = list(where)
    count_params = dict(params)
    clean_exclude_ids = [str(item_id).strip() for item_id in (exclude_ids or []) if str(item_id).strip()][:200]
    if clean_exclude_ids:
        where.append("NOT (i.id::text = ANY(%(exclude_ids)s))")
        params["exclude_ids"] = clean_exclude_ids
    safe_offset = max(0, int(offset or 0))
    safe_limit = max(1, min(int(limit or 50), 200))
    if search:
        search_read_model_result = _query_feed_by_platform_search_read_model(
            schema=schema,
            platform=platform,
            offset=safe_offset,
            limit=safe_limit,
            source=source,
            group=group,
            category=category,
            search=search,
            exclude_ids=clean_exclude_ids,
            cursor=cursor,
            user_id=user_id,
            public_only=public_only,
            manual_owner_user_id=manual_owner_user_id,
            min_github_stars=min_github_stars,
        )
        if search_read_model_result is not None:
            return search_read_model_result
        platform_scope = _info_platform_scope(platform=platform, source=source, group=group, category=category)
        if (
            not (platform == "manual" and manual_owner_user_id)
            and platform_scope
            # ENG-0710: group_source scope 已不物化,搜索组合放行到下方 live(无 circuit 门控)
            and platform_scope[0] != "group_source"
            and _can_use_info_search_read_model(
                search=search,
                user_id=user_id,
                public_only=public_only,
                min_github_stars=min_github_stars,
            )
        ):
            return _degraded_feed_platform_page_result(
                platform,
                category=category,
                reason="info_search_read_model_unavailable",
            )
    read_model_result = _query_feed_by_platform_read_model(
        schema=schema,
        platform=platform,
        offset=safe_offset,
        limit=safe_limit,
        source=source,
        group=group,
        category=category,
        search=search,
        exclude_ids=clean_exclude_ids,
        cursor=cursor,
        user_id=user_id,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    if read_model_result is not None:
        return read_model_result
    count_cache_key = _platform_page_count_cache_key(
        schema=schema,
        platform=platform,
        source=source,
        group=group,
        category=category,
        search=search,
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        user_id=user_id,
        min_github_stars=min_github_stars,
    )
    cached_total = _cache_get(count_cache_key)
    total = int(cached_total) if cached_total is not None else None
    total_is_estimate = False
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 4500)
            items = _fetch_items(
                conn,
                schema,
                where,
                params,
                order_sql=(
                    "ORDER BY COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST, "
                    "i.fetched_at DESC NULLS LAST, i.relevance_score DESC NULLS LAST, i.id DESC"
                ),
                limit=safe_limit,
                offset=safe_offset,
                status_user_id=user_id,
            )
            if total is None:
                try:
                    total = _count_items(conn, schema, count_where, count_params)
                    _cache_set(count_cache_key, total)
                except Exception:
                    _rollback_safely(conn)
                    total = _estimate_platform_page_total(
                        offset=safe_offset,
                        limit=safe_limit,
                        item_count=len(items),
                    )
                    total_is_estimate = True
    except Exception:
        raise RemoteDBError("platform page query failed")
    next_offset = safe_offset + len(items)
    result = {
        "items": items,
        "platform": platform,
        "category": category,
        "total": total,
        "offset": safe_offset,
        "limit": safe_limit,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
        "data_backend": feed_read_backend(),
    }
    if total_is_estimate:
        result.update({
            "degraded": True,
            "degraded_reason": "platform_page_total_unavailable",
            "total_is_estimate": True,
        })
    return result


def get_feed_item(
    *,
    item_id: str,
    public_only: bool = False,
    can_access_all: bool = False,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict | None:
    schema = remote_schema()
    cache_key = (
        "feed_item_detail",
        schema,
        item_id,
        bool(public_only),
        bool(can_access_all),
        user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=None if can_access_all else user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.id = %(item_id)s")
    params["item_id"] = item_id
    with connect() as conn:
        rows = _fetch_items(
            conn,
            schema,
            where,
            params,
            order_sql="",
            limit=1,
            detail=True,
            status_user_id=user_id,
        )
    if not rows:
        return None
    item = rows[0]
    item["data_backend"] = feed_read_backend()
    return _cache_set_copy(cache_key, item)


def get_feed_items(
    *,
    item_ids: list[str],
    public_only: bool = False,
    can_access_all: bool = False,
    user_id: str | None = None,
    min_github_stars: int = 50,
) -> list[dict]:
    ids = []
    seen: set[str] = set()
    for raw in item_ids:
        item_id = str(raw).strip()
        if not item_id or item_id in seen:
            continue
        ids.append(item_id)
        seen.add(item_id)
    if not ids:
        return []
    schema = remote_schema()
    cache_key = (
        "feed_items_detail_batch",
        schema,
        tuple(ids),
        bool(public_only),
        bool(can_access_all),
        user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=None if can_access_all else user_id,
        min_github_stars=min_github_stars,
    )
    where.append("i.id = ANY(%(item_ids)s)")
    params["item_ids"] = ids
    with connect() as conn:
        rows = _fetch_items(
            conn,
            schema,
            where,
            params,
            order_sql="",
            detail=True,
            status_user_id=user_id,
        )
    by_id = {str(item.get("id")): item for item in rows}
    ordered = []
    for item_id in ids:
        item = by_id.get(item_id)
        if not item:
            continue
        item["data_backend"] = feed_read_backend()
        ordered.append(item)
    return _cache_set_copy(cache_key, ordered)
