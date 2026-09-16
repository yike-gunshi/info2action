from __future__ import annotations


def storage_mode() -> str:
    raw = _normalized(_runtime_env().get(STORAGE_MODE_ENV))
    if raw in {"", STORAGE_LOCAL, "sqlite"}:
        return STORAGE_LOCAL
    mode = raw.replace("-", "_")
    if mode in {"remote", "remoteonly", STORAGE_REMOTE_ONLY}:
        return STORAGE_REMOTE_ONLY
    if mode in {"sync", "sqlite_sync", "sqlite_then_sync", "local_then_sync"}:
        return STORAGE_SQLITE_THEN_SYNC
    raise RemoteDBConfigError(
        f"Invalid {STORAGE_MODE_ENV}: {raw!r}. "
        f"Use '{STORAGE_LOCAL}', '{STORAGE_SQLITE_THEN_SYNC}', or '{STORAGE_REMOTE_ONLY}'."
    )


def assert_storage_contract_ready() -> dict[str, Any]:
    """Validate the high-level storage contract for deployments.

    ``remote_only`` is the final target: a cloned repo plus Supabase credentials
    can access and mutate all production data without a local ``data/feed.db``.
    Until every persistent table and pipeline writer is migrated, this mode must
    fail loudly rather than falling back to SQLite.
    """
    mode = storage_mode()
    if mode == STORAGE_LOCAL:
        return {"mode": mode, "remote_only": False, "blockers": []}
    if mode == STORAGE_SQLITE_THEN_SYNC:
        return {"mode": mode, "remote_only": False, "blockers": []}

    blockers = remote_only_blockers()
    if blockers:
        raise RemoteDBConfigError(
            f"{STORAGE_MODE_ENV}={STORAGE_REMOTE_ONLY} remote-only target is not ready. "
            f"Known blockers: {'; '.join(blockers)}"
        )
    return {
        "mode": mode,
        "remote_only": True,
        "blockers": [],
        "asset_storage": assert_asset_storage_ready(),
    }


def status_write_to_remote() -> bool:
    return status_backend() in REMOTE_BACKENDS


def app_state_to_remote() -> bool:
    return app_state_backend() == "supabase"


def asset_storage_to_remote() -> bool:
    return asset_backend() == "supabase"


def any_remote_backend_enabled() -> bool:
    return events_read_from_remote() or feed_read_from_remote() or status_write_to_remote()


def _info_read_model_prewarm_page_limit(env: dict[str, str] | None = None) -> int:
    return min(
        _env_int(
            env or _runtime_env(),
            INFO_READ_MODEL_PREWARM_PAGE_LIMIT_ENV,
            INFO_READ_MODEL_PREWARM_PAGE_LIMIT_DEFAULT,
            min_value=1,
        ),
        200,
    )


def _info_read_model_prewarm_pages_per_scope(env: dict[str, str] | None = None) -> int:
    return min(
        _env_int(
            env or _runtime_env(),
            INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_ENV,
            INFO_READ_MODEL_PREWARM_PAGES_PER_SCOPE_DEFAULT,
            min_value=1,
        ),
        5,
    )


def prewarm_events_categories() -> dict[str, Any]:
    """BF-0515-prewarm-events: warm fetch_events cache for default + each L1 category.

    Each pill the user clicks corresponds to a `categories=[X]` cache key.
    Without prewarm, first user to click each category pays the full cold
    cost (~1-4s). After prewarm, the cache is hot.

    Sequential to avoid overloading the connection pool. Total time ~10-25s
    for 14 calls (13 categories + default).
    """
    from category_taxonomy import ACTIVE_CATEGORY_IDS  # local import to avoid circular

    timings: list[dict[str, Any]] = []
    t_total = time.time()
    # 1 default (no categories filter)
    targets: list[list[str] | None] = [None]
    # + each single L1 category (matches what pill click sends)
    for cat in ACTIVE_CATEGORY_IDS:
        targets.append([cat])

    success = 0
    failed = 0
    for cats in targets:
        t0 = time.time()
        try:
            fetch_events(
                page=1,
                limit=20,
                user_id=None,
                public_only=True,
                min_github_stars=50,
                enabled=True,
                categories=cats,
            )
            timings.append({"cats": cats or "default", "ms": int((time.time() - t0) * 1000), "ok": True})
            success += 1
        except Exception as exc:
            timings.append({"cats": cats or "default", "ms": int((time.time() - t0) * 1000), "ok": False, "err": str(exc)[:80]})
            failed += 1
    return {
        "ok": failed == 0,
        "total_ms": int((time.time() - t_total) * 1000),
        "success": success,
        "failed": failed,
        "per_target": timings,
    }


def prewarm_platforms(
    *,
    refresh_read_model: bool | None = None,
    refresh_read_model_min_interval_sec: int = 600,
    refresh_highlights_read_model: bool | None = None,
    refresh_highlights_read_model_min_interval_sec: int = 600,
) -> dict[str, Any]:
    """Warm up the in-process result cache for /api/feed/platforms.

    Sequence:
      1. Call query_feed_sections / query_feed_platforms with anonymous params
         → populates result_cache for the 信息 tab default views.
      2. Result cache TTL is _feed_result_cache_ttl_sec() (default 900s) —
         long enough for periodic prewarm to refresh it before expiry.

    Called from:
      - lifespan startup (background thread)
      - fetch.py finally block (background thread, after every fetch_run)

    perf-v27 P0: platform MV(mv_items_top_per_platform)已删除——它自
    BF-0515 起无任何读取方,每轮刷新却写 ~87GB 临时文件。
    """
    timings = {}
    t0 = time.time()
    t1 = time.time()
    if refresh_read_model is None:
        env = _runtime_env()
        refresh_read_model = _info_read_model_enabled(env) and _truthy(env.get(INFO_READ_MODEL_REFRESH_ENV, "1"))
    if refresh_read_model:
        try:
            read_model_result = refresh_info_read_model_if_stale(
                min_interval_sec=refresh_read_model_min_interval_sec
            )
            timings['read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['read_model_refresh_ok'] = read_model_result.get('ok', False)
            if read_model_result.get("skipped"):
                timings['read_model_refresh_skipped_reason'] = read_model_result.get("skipped")
            if read_model_result.get("scope_items") is not None:
                timings['read_model_scope_items'] = read_model_result.get("scope_items")
        except Exception as exc:
            timings['read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['read_model_refresh_ok'] = False
            timings['read_model_refresh_error'] = str(exc)[:200]
    else:
        timings['read_model_refresh_ms'] = 0
        timings['read_model_refresh_ok'] = False
        timings['read_model_refresh_skipped'] = True

    t1 = time.time()
    if refresh_highlights_read_model is None:
        env = _runtime_env()
        refresh_highlights_read_model = (
            _highlights_read_model_enabled(env)
            and _truthy(env.get(HIGHLIGHTS_READ_MODEL_REFRESH_ENV, "1"))
        )
    if refresh_highlights_read_model:
        try:
            highlights_result = refresh_highlights_read_model_if_stale(
                min_interval_sec=refresh_highlights_read_model_min_interval_sec
            )
            timings['highlights_read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['highlights_read_model_refresh_ok'] = highlights_result.get('ok', False)
            if highlights_result.get("skipped"):
                timings['highlights_read_model_refresh_skipped_reason'] = highlights_result.get("skipped")
            if highlights_result.get("scope_items") is not None:
                timings['highlights_read_model_scope_items'] = highlights_result.get("scope_items")
        except Exception as exc:
            timings['highlights_read_model_refresh_ms'] = int((time.time() - t1) * 1000)
            timings['highlights_read_model_refresh_ok'] = False
            timings['highlights_read_model_refresh_error'] = str(exc)[:200]
    else:
        timings['highlights_read_model_refresh_ms'] = 0
        timings['highlights_read_model_refresh_ok'] = False
        timings['highlights_read_model_refresh_skipped'] = True

    t1 = time.time()
    try:
        # 对齐 Wave2 首屏每组条数,使 prewarm 预热的缓存条目正是路由首屏请求命中的那个。
        query_feed_sections(
            per_category=_feed_first_paint_per_group(),
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=50,
        )
        timings['sections_query_ms'] = int((time.time() - t1) * 1000)
        timings['sections_query_ok'] = True
    except Exception as exc:
        timings['sections_query_ms'] = int((time.time() - t1) * 1000)
        timings['sections_query_ok'] = False
        timings['sections_query_error'] = str(exc)[:200]

    t1 = time.time()
    try:
        query_feed_platforms(
            per_platform=_feed_first_paint_per_group(),
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=50,
        )
        timings['query_ms'] = int((time.time() - t1) * 1000)
        timings['query_ok'] = True
    except Exception as exc:
        timings['query_ms'] = int((time.time() - t1) * 1000)
        timings['query_ok'] = False
        timings['query_error'] = str(exc)[:200]
    t2 = time.time()
    if _info_read_model_enabled():
        try:
            env = _runtime_env()
            # 对齐 Wave2 首屏 20 条 + A-3 已修 /more 按需,故页预热从 ~600 条降到 ~20/类,砍掉周期性 20-30s 预热与内存尖峰;prewarm 是纯后台优化,/more 仍可按需取,零功能影响。
            page_result = prewarm_info_read_model_pages(
                max_scopes=_env_int(
                    env,
                    INFO_READ_MODEL_PREWARM_SCOPES_ENV,
                    INFO_READ_MODEL_PREWARM_SCOPES_DEFAULT,
                    min_value=1,
                ),
                page_limit=_info_read_model_prewarm_page_limit(env),
                pages_per_scope=_info_read_model_prewarm_pages_per_scope(env),
            )
            timings['read_model_page_prewarm_ms'] = int((time.time() - t2) * 1000)
            timings['read_model_page_prewarm_ok'] = page_result.get('ok', False)
            if page_result.get("pages") is not None:
                timings['read_model_page_prewarm_pages'] = page_result.get("pages")
            if page_result.get("items") is not None:
                timings['read_model_page_prewarm_items'] = page_result.get("items")
        except Exception as exc:
            timings['read_model_page_prewarm_ms'] = int((time.time() - t2) * 1000)
            timings['read_model_page_prewarm_ok'] = False
            timings['read_model_page_prewarm_error'] = str(exc)[:200]
    else:
        timings['read_model_page_prewarm_ms'] = 0
        timings['read_model_page_prewarm_skipped'] = True
    timings['total_ms'] = int((time.time() - t0) * 1000)
    return timings


def _prune_info_read_model_versions(
    conn: Any,
    *,
    schema: str,
    retain_complete_versions: int = INFO_READ_MODEL_RETAIN_COMPLETE_VERSIONS,
) -> None:
    """Prune stale Info read model versions without deleting the active version."""
    safe_retain = max(1, int(retain_complete_versions or 1))
    conn.execute(
        f"""WITH protected_versions AS (
               SELECT active_version_id AS version_id
                 FROM {schema}.info_read_model_state
                WHERE active_version_id IS NOT NULL
               UNION
               SELECT version_id
                 FROM (
                   SELECT version_id
                     FROM {schema}.info_read_model_versions
                    WHERE status = 'complete'
                    ORDER BY completed_at DESC NULLS LAST,
                             generated_at DESC NULLS LAST
                    LIMIT %(retain_complete_versions)s
                 ) recent_complete
             )
             DELETE FROM {schema}.info_read_model_versions v
              WHERE NOT EXISTS (
                    SELECT 1
                      FROM protected_versions p
                     WHERE p.version_id = v.version_id
                )
                AND (
                     v.status = 'complete'
                  OR v.generated_at < now() - interval '{INFO_READ_MODEL_PRUNE_TRANSIENT_AGE_HOURS} hours'
                )""",
        {"retain_complete_versions": safe_retain},
    )


def migrate_info_read_model_sort_policy() -> dict[str, Any]:
    """Rerank the active info read model when only the sort policy changed.

    This avoids a full card_json rebuild for legacy versions whose cards are
    still usable but whose scope ranks were generated with an older policy.
    """
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
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
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "read_active_version"
            step_t0 = time.time()
            active = _info_read_model_active_version(conn, schema)
            if not active or not active.get("version_id"):
                return {"ok": False, "skipped": "no_active_version"}
            active_meta = _json_value(active.get("meta_json"))
            if isinstance(active_meta, dict) and active_meta.get("sort_policy") == INFO_READ_MODEL_SORT_POLICY:
                return {
                    "ok": True,
                    "skipped": "sort_policy_current",
                    "version_id": str(active["version_id"]),
                }
            active_version_id = str(active["version_id"])
            _record_step(current_step, step_t0)

            current_step = "normalize_card_sort_at"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_card_items
                       SET sort_at = COALESCE(published_at, fetched_at, sort_at)
                     WHERE version_id = %(active_version_id)s::uuid
                       AND sort_at IS DISTINCT FROM COALESCE(published_at, fetched_at, sort_at)""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_reranked_scope_items"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.info_read_model_reranked_scope_items")
            conn.execute(
                f"""CREATE TEMP TABLE info_read_model_reranked_scope_items ON COMMIT DROP AS
                    WITH source_rows AS (
                      SELECT si.scope_key,
                             si.item_id,
                             COALESCE(ci.sort_at, ci.published_at, ci.fetched_at, si.sort_at, si.fetched_at) AS sort_at,
                             COALESCE(ci.fetched_at, si.fetched_at) AS fetched_at,
                             COALESCE(ci.relevance_score, si.relevance_score) AS relevance_score
                        FROM {schema}.info_scope_items si
                        JOIN {schema}.info_card_items ci
                          ON ci.version_id = si.version_id
                         AND ci.item_id = si.item_id
                       WHERE si.version_id = %(active_version_id)s::uuid
                    )
                    SELECT scope_key,
                           row_number() OVER (
                             PARTITION BY scope_key
                             ORDER BY sort_at DESC NULLS LAST,
                                      fetched_at DESC NULLS LAST,
                                      relevance_score DESC NULLS LAST,
                                      item_id DESC
                           )::integer AS rank,
                           item_id,
                           sort_at,
                           fetched_at,
                           relevance_score
                      FROM source_rows""",
                {"active_version_id": active_version_id},
            )
            conn.execute("ANALYZE pg_temp.info_read_model_reranked_scope_items")
            reranked_row = conn.execute(
                "SELECT count(*) AS n FROM pg_temp.info_read_model_reranked_scope_items"
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "update_scopes"
            step_t0 = time.time()
            conn.execute(
                f"""WITH agg AS (
                       SELECT scope_key,
                              count(*)::integer AS total_count,
                              max(sort_at) AS max_sort_at
                         FROM pg_temp.info_read_model_reranked_scope_items
                        GROUP BY scope_key
                     )
                     UPDATE {schema}.info_scopes sc
                        SET total_count = agg.total_count,
                            max_sort_at = agg.max_sort_at,
                            generated_at = now()
                       FROM agg
                      WHERE sc.version_id = %(active_version_id)s::uuid
                        AND sc.scope_key = agg.scope_key""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "replace_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"DELETE FROM {schema}.info_scope_items WHERE version_id = %(active_version_id)s::uuid",
                {"active_version_id": active_version_id},
            )
            conn.execute(
                f"""INSERT INTO {schema}.info_scope_items (
                       version_id, scope_key, rank, item_id, sort_at, fetched_at, relevance_score
                     )
                     SELECT %(active_version_id)s::uuid, scope_key, rank, item_id,
                            sort_at, fetched_at, relevance_score
                       FROM pg_temp.info_read_model_reranked_scope_items""",
                {"active_version_id": active_version_id},
            )
            _record_step(current_step, step_t0)

            current_step = "mark_version_policy"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.info_read_model_versions
                       SET meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'sort_policy', %(sort_policy)s::text,
                             'sort_policy_migration', 'rerank_active',
                             'sort_policy_migrated_at', now()
                           ),
                           completed_at = COALESCE(completed_at, now())
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    "active_version_id": active_version_id,
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

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"info read model sort policy migration failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "sort_policy_migration",
        "version_id": active_version_id,
        "sort_policy": INFO_READ_MODEL_SORT_POLICY,
        "scope_items": int((reranked_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def _events_read_model_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_ENV,
        EVENTS_READ_MODEL_STATEMENT_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _context_search_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    return _env_int(
        env or _runtime_env(),
        CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_ENV,
        CONTEXT_SEARCH_STATEMENT_TIMEOUT_MS_DEFAULT,
        min_value=500,
    )


def _context_search_events_only_statement_timeout_ms(env: dict[str, str] | None = None) -> int:
    env = env or _runtime_env()
    return min(
        _context_search_statement_timeout_ms(env),
        _env_int(
            env,
            CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_ENV,
            CONTEXT_SEARCH_EVENTS_ONLY_STATEMENT_TIMEOUT_MS_DEFAULT,
            min_value=500,
        ),
    )


def _set_short_statement_timeout(conn: Any, timeout_ms: int = _REMOTE_STATUS_TIMEOUT_MS) -> None:
    try:
        conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_ms)}ms'")
    except Exception:
        _rollback_safely(conn)


def _set_local_statement_and_idle_tx_timeouts(
    conn: Any,
    *,
    statement_timeout_ms: int,
    idle_tx_timeout_ms: int,
) -> bool:
    try:
        conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_ms)}ms'")
        conn.execute(
            "SET LOCAL idle_in_transaction_session_timeout = "
            f"'{int(idle_tx_timeout_ms)}ms'"
        )
        return True
    except Exception:
        _rollback_safely(conn)
        return False


def supabase_storage_bucket() -> str:
    return (_runtime_env().get(SUPABASE_STORAGE_BUCKET_ENV) or DEFAULT_STORAGE_BUCKET).strip()


def assert_asset_storage_ready() -> dict[str, Any]:
    if not asset_storage_to_remote():
        return {"backend": STORAGE_LOCAL, "remote_assets": False}
    supabase_project_url()
    supabase_service_role_key()
    bucket = supabase_storage_bucket()
    return {"backend": "supabase", "bucket": bucket, "remote_assets": True}


def remote_db_pressure(
    *,
    timeout_minutes: int | None = None,
    autovacuum_age_sec: int | None = None,
    probe_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Read-only pressure probe used to skip optional DB-heavy work."""
    env = _runtime_env()
    safe_timeout_minutes = (
        int(timeout_minutes)
        if timeout_minutes is not None
        else _env_int(env, REMOTE_DB_PRESSURE_TIMEOUT_MIN_ENV, 15, min_value=1)
    )
    safe_autovacuum_age_sec = (
        int(autovacuum_age_sec)
        if autovacuum_age_sec is not None
        else _env_int(env, REMOTE_DB_PRESSURE_AUTOVACUUM_AGE_SEC_ENV, 1800, min_value=1)
    )
    safe_probe_timeout_ms = (
        int(probe_timeout_ms)
        if probe_timeout_ms is not None
        else _env_int(env, REMOTE_DB_PRESSURE_PROBE_TIMEOUT_MS_ENV, 1500, min_value=100)
    )
    t0 = time.time()
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    try:
        schema = remote_schema()
        with connect() as conn:
            _set_short_statement_timeout(conn, safe_probe_timeout_ms)
            running_row = conn.execute(
                f"""SELECT EXISTS (
                        SELECT 1
                          FROM {schema}.fetch_runs
                         WHERE status = 'running'
                           AND started_at >= now() - interval '3 hours'
                           AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at)
                               >= now() - (%s::int * interval '1 second')
                         LIMIT 1
                     ) AS has_running""",
                (fetch_run_heartbeat_grace_seconds(),),
            ).fetchone()
            if bool(_row_get(running_row, "has_running", False)):
                reasons.append("remote_fetch_running")

            timeout_row = conn.execute(
                f"""SELECT EXISTS (
                        SELECT 1
                          FROM {schema}.fetch_runs
                         WHERE started_at >= now() - (%s::int * interval '1 minute')
                           AND (
                             COALESCE(error_msg, '') ILIKE '%%statement timeout%%'
                             OR COALESCE(stats_json::text, '') ILIKE '%%statement timeout%%'
                           )
                         LIMIT 1
                     ) AS has_recent_timeout""",
                (safe_timeout_minutes,),
            ).fetchone()
            if bool(_row_get(timeout_row, "has_recent_timeout", False)):
                reasons.append("recent_statement_timeout")

            vacuum_row = conn.execute(
                """SELECT COUNT(*)::int AS active_vacuums,
                          COALESCE(MAX(EXTRACT(EPOCH FROM (now() - a.query_start))), 0)::float
                            AS max_autovacuum_age_sec
                     FROM pg_stat_progress_vacuum v
                     LEFT JOIN pg_stat_activity a ON a.pid = v.pid
                    WHERE COALESCE(a.backend_type, '') = 'autovacuum worker'"""
            ).fetchone()
            active_vacuums = int(_row_get(vacuum_row, "active_vacuums", 0) or 0)
            max_age = float(_row_get(vacuum_row, "max_autovacuum_age_sec", 0) or 0)
            detail["active_vacuums"] = active_vacuums
            detail["max_autovacuum_age_sec"] = round(max_age, 1)
            if active_vacuums > 0 and max_age >= safe_autovacuum_age_sec:
                reasons.append("long_autovacuum")
    except Exception as exc:
        return {
            "ok": False,
            "pressure": True,
            "reasons": ["pressure_probe_failed"],
            "error": str(exc)[:200],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    return {
        "ok": True,
        "pressure": bool(reasons),
        "reasons": reasons,
        "detail": detail,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _asr_today_cst() -> str:
    """Return the Beijing-date bucket used by ASR quota accounting."""
    cst = datetime.now(timezone.utc) + timedelta(hours=8)
    return cst.strftime("%Y-%m-%d")


def _asr_daily_quota_sec() -> int:
    hours = float(_runtime_env().get("ASR_DAILY_QUOTA_HOURS") or ASR_DAILY_QUOTA_HOURS_DEFAULT)
    return int(hours * 3600)


def _asr_usage_snapshot(date_cst: str, seconds_used: int) -> dict[str, Any]:
    daily_sec = _asr_daily_quota_sec()
    try:
        next_day = (datetime.strptime(date_cst, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        reset_at = f"{next_day}T00:00:00+08:00"
    except Exception:
        reset_at = None
    return {
        "date_cst": date_cst,
        "seconds_used": int(seconds_used or 0),
        "used_hours": round(int(seconds_used or 0) / 3600, 1),
        "daily_quota_sec": daily_sec,
        "remaining_hours": round((daily_sec - int(seconds_used or 0)) / 3600, 1),
        "over_limit": int(seconds_used or 0) >= daily_sec,
        "reset_at": reset_at,
    }


def get_asr_usage_today_remote(pg_conn: Any | None = None, user_id: str | int = "0") -> dict[str, Any]:
    """Return today's ASR quota usage from Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return get_asr_usage_today_remote(conn, user_id=user_id)
    today = _asr_today_cst()
    row = pg_conn.execute(
        f"""SELECT seconds_used
              FROM {remote_schema()}.asr_usage
             WHERE user_id = %s AND date_cst = %s""",
        (str(user_id), today),
    ).fetchone()
    used = int(_row_get(row, "seconds_used", 0) or 0) if row else 0
    return _asr_usage_snapshot(today, used)


def check_asr_quota_remote(
    pg_conn: Any | None,
    duration_sec: int,
    *,
    user_id: str | int = "0",
) -> tuple[bool, dict[str, Any]]:
    """Check ASR quota against the remote usage table."""
    usage = get_asr_usage_today_remote(pg_conn, user_id=user_id)
    allowed = usage["seconds_used"] + max(0, int(duration_sec or 0)) <= usage["daily_quota_sec"]
    return allowed, usage


def consume_asr_quota_remote(
    pg_conn: Any | None,
    duration_sec: int,
    *,
    user_id: str | int = "0",
) -> dict[str, Any]:
    """Increment ASR quota usage in Supabase and return the updated snapshot."""
    if duration_sec is None or duration_sec <= 0:
        return get_asr_usage_today_remote(pg_conn, user_id=user_id)
    if pg_conn is None:
        with connect() as conn:
            return consume_asr_quota_remote(conn, duration_sec, user_id=user_id)
    today = _asr_today_cst()
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.asr_usage
              (user_id, date_cst, seconds_used, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (user_id, date_cst) DO UPDATE SET
              seconds_used = {remote_schema()}.asr_usage.seconds_used + excluded.seconds_used,
              updated_at = excluded.updated_at""",
        (str(user_id), today, int(duration_sec)),
    )
    _commit_if_supported(pg_conn)
    return get_asr_usage_today_remote(pg_conn, user_id=user_id)


def try_consume_generation_quota_remote(
    pg_conn: Any | None,
    *,
    user_id: str,
    limit: int,
) -> tuple[bool, dict[str, Any]]:
    """Atomically consume one generation credit if under `limit`.

    Uses an upsert whose DO UPDATE is guarded by a WHERE on the existing count,
    so a row at the limit yields no RETURNING row (not consumed). New rows insert
    at count=1. Returns (allowed, snapshot).
    """
    if pg_conn is None:
        with connect() as conn:
            return try_consume_generation_quota_remote(conn, user_id=user_id, limit=limit)
    today = _asr_today_cst()
    schema = remote_schema()
    row = pg_conn.execute(
        f"""INSERT INTO {schema}.user_daily_generation
              (user_id, day_cst, count, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (user_id, day_cst) DO UPDATE SET
              count = {schema}.user_daily_generation.count + 1,
              updated_at = now()
            WHERE {schema}.user_daily_generation.count < %s
            RETURNING count""",
        (str(user_id), today, int(limit)),
    ).fetchone()
    _commit_if_supported(pg_conn)
    if row is not None:
        return True, _generation_usage_snapshot(today, int(_row_get(row, "count", 1) or 1), limit)
    return False, get_generation_usage_today_remote(pg_conn, user_id=user_id, limit=limit)


def get_item_asr_state_remote(item_id: str) -> dict[str, Any] | None:
    """Fetch the ASR status payload used by `/api/items/{id}/asr`."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, asr_text, asr_status, asr_duration_sec,
                      asr_cost_yuan, asr_attempted_at, asr_failed_reason, asr_provider,
                      ai_summary, asr_segments, asr_text_cn, asr_segments_cn
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    for col in ASR_JSON_COLUMNS:
        data[col] = _json_value(data.get(col))
    data["asr_attempted_at"] = _timestamp_value(data.get("asr_attempted_at"))
    return data


def get_asr_worker_item_remote(item_id: str) -> dict[str, Any] | None:
    """Fetch the item fields needed by the ASR worker."""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, title, content, ai_summary, media_json, url,
                      asr_text, asr_duration_sec
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["media_json"] = _json_value(data.get("media_json"))
    return data


def get_pending_asr_item_ids_remote(item_ids: list[str]) -> list[str]:
    """Return requested remote items that have never entered the ASR state machine."""
    ids = list(dict.fromkeys(str(item_id) for item_id in item_ids if item_id))
    if not ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id
                  FROM {remote_schema()}.items
                 WHERE id = ANY(%s)
                   AND asr_status IS NULL""",
            (ids,),
        ).fetchall()
    found = {str(row["id"] if isinstance(row, dict) else row[0]) for row in rows}
    return [item_id for item_id in ids if item_id in found]


def cluster_ids_for_item_remote(item_id: str) -> list[int]:
    """Return clusters whose summaries may depend on this item's transcript."""
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT cluster_id
                  FROM {remote_schema()}.cluster_items
                 WHERE item_id = %s
                 ORDER BY cluster_id""",
            (item_id,),
        ).fetchall()
    return [
        int(row["cluster_id"] if isinstance(row, dict) else row[0])
        for row in rows
    ]


def update_item_asr_fields_remote(item_id: str, **fields: Any) -> None:
    """Update ASR-related item fields in Supabase."""
    updates = {key: value for key, value in fields.items() if key in ASR_ITEM_UPDATE_COLUMNS}
    if not updates:
        return
    sets = []
    params: dict[str, Any] = {"item_id": item_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        if key in ASR_JSON_COLUMNS:
            params[pname] = _maybe_jsonb(value)
        elif key in ASR_TIMESTAMP_COLUMNS:
            params[pname] = _timestamp_value(value)
        else:
            params[pname] = value
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.items SET {', '.join(sets)} WHERE id = %(item_id)s",
            params,
        )
        conn.commit()
    clear_item_detail_cache_keys(item_id)
    if "ai_summary" in updates:
        clear_feed_cache_keys()


def _storage_object_url(object_path: str) -> str:
    from urllib.parse import quote

    clean_path = object_path.strip().lstrip("/")
    if not clean_path or ".." in clean_path.split("/"):
        raise RemoteDBConfigError(f"Invalid storage object path: {object_path!r}")
    bucket = quote(supabase_storage_bucket(), safe="")
    encoded_path = "/".join(quote(part, safe="") for part in clean_path.split("/"))
    return f"{supabase_project_url()}/storage/v1/object/{bucket}/{encoded_path}"


def _storage_headers(content_type: str | None = None, *, upsert: bool = False) -> dict[str, str]:
    key = supabase_service_role_key()
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    if content_type:
        headers["Content-Type"] = content_type
    if upsert:
        headers["x-upsert"] = "true"
    return headers


def upload_asset_bytes_remote(
    object_path: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    upsert: bool = True,
    source_item_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Upload binary data to Supabase Storage and record lightweight metadata."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        data=data,
        method="POST" if upsert else "PUT",
        headers=_storage_headers(content_type, upsert=upsert),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RemoteDBError(f"Supabase Storage upload failed: HTTP {exc.code} {body}") from exc
    upsert_asset_metadata_remote(
        object_path=object_path,
        content_type=content_type,
        size_bytes=len(data),
        source_item_id=source_item_id,
        kind=kind,
    )
    return {
        "bucket": supabase_storage_bucket(),
        "object_path": object_path,
        "content_type": content_type,
        "size_bytes": len(data),
    }


def download_asset_bytes_remote(object_path: str) -> bytes | None:
    """Download binary data from Supabase Storage. Return None when missing."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        method="GET",
        headers=_storage_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        missing = (
            exc.code == 404
            or "not_found" in body.lower()
            or "object not found" in body.lower()
        )
        if missing:
            return None
        raise RemoteDBError(f"Supabase Storage download failed: HTTP {exc.code} {body}") from exc


def delete_asset_remote(object_path: str) -> None:
    """Delete a remote binary asset and its metadata."""
    req = urllib.request.Request(
        _storage_object_url(object_path),
        method="DELETE",
        headers=_storage_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code != 404 and "not_found" not in body.lower() and "object not found" not in body.lower():
            raise RemoteDBError(f"Supabase Storage delete failed: HTTP {exc.code} {body}") from exc
    with connect() as conn:
        conn.execute(
            f"DELETE FROM {remote_schema()}.remote_assets WHERE object_path = %s",
            (object_path,),
        )
        conn.commit()


def upsert_asset_metadata_remote(
    *,
    object_path: str,
    content_type: str | None = None,
    size_bytes: int | None = None,
    source_item_id: str | None = None,
    kind: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.remote_assets
                  (object_path, bucket, content_type, size_bytes, source_item_id, kind, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (object_path) DO UPDATE SET
                  bucket = excluded.bucket,
                  content_type = excluded.content_type,
                  size_bytes = excluded.size_bytes,
                  source_item_id = COALESCE(excluded.source_item_id, {remote_schema()}.remote_assets.source_item_id),
                  kind = COALESCE(excluded.kind, {remote_schema()}.remote_assets.kind),
                  updated_at = excluded.updated_at""",
            (
                object_path,
                supabase_storage_bucket(),
                content_type,
                size_bytes,
                source_item_id,
                kind,
            ),
        )
        conn.commit()


def _item_status_join(schema: str, user_id: str | None) -> tuple[str, dict[str, Any], str | None]:
    if not user_id:
        return "", {}, None
    return (
        f"LEFT JOIN {schema}.item_status s ON s.item_id = i.id AND s.user_id = %(status_user_id)s",
        {"status_user_id": user_id},
        "s",
    )


def prewarm_info_read_model_pages(*, max_scopes: int = 5, page_limit: int = 50, pages_per_scope: int = 2) -> dict[str, Any]:
    if not _info_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_max_scopes = max(1, min(int(max_scopes or 80), 300))
    safe_limit = max(1, min(int(page_limit or 50), 200))
    safe_pages_per_scope = max(1, min(int(pages_per_scope or 1), 5))
    safe_item_limit = safe_limit * safe_pages_per_scope
    t0 = time.time()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 30000)
            active = _info_read_model_active_version(conn, schema)
            if not active:
                return {"ok": False, "skipped": "no_active_version"}
            version_id = active["version_id"]
            rows = conn.execute(
                f"""WITH ranked_scopes AS (
                       -- perf-v27 P4: 只剩 section_category 维度(其余维度已不物化,
                       -- 走 live)。此查询原是 info 侧最重循环负载(生产实测均耗
                       -- 14.9-16.35s/峰值顶满 30s 超时);收缩后仅 ~14 个 scope ×
                       -- TOP_N 行,退化为平凡查询。
                       SELECT scope_key, platform, dimension, value, total_count,
                              row_number() OVER (
                                ORDER BY total_count DESC, scope_key
                              ) AS scope_rank
                         FROM {schema}.info_scopes
                        WHERE version_id = %(version_id)s
                          AND dimension = 'section_category'
                          AND total_count > 0
                     ),
                     hot_scopes AS (
                       SELECT scope_key, platform, dimension, value, total_count
                         FROM ranked_scopes
                        WHERE scope_rank <= %(max_scopes)s
                     )
                     SELECT hs.scope_key, hs.platform, hs.dimension, hs.value,
                            hs.total_count, page.rank, page.card_json
                       FROM hot_scopes hs
                       CROSS JOIN LATERAL (
                             SELECT ordered.order_rank AS rank, ordered.card_json
                               FROM (
                                     SELECT row_number() OVER (
                                              ORDER BY {_info_scope_item_order_sql("si")}
                                            ) AS order_rank,
                                            ci.card_json,
                                            si.sort_at,
                                            si.fetched_at,
                                            si.relevance_score,
                                            si.item_id
                                       FROM {schema}.info_scope_items si
                                       JOIN {schema}.info_card_items ci
                                         ON ci.version_id = si.version_id
                                        AND ci.item_id = si.item_id
                                      WHERE si.version_id = %(version_id)s
                                        AND si.scope_key = hs.scope_key
                                        AND {_info_display_source_filter("ci")}
                                    ) ordered
                              ORDER BY ordered.order_rank
                              LIMIT %(item_limit)s
                           ) page
                      ORDER BY hs.total_count DESC, hs.scope_key, page.rank""",
                {
                    "version_id": version_id,
                    "max_scopes": safe_max_scopes,
                    "item_limit": safe_item_limit,
                },
            ).fetchall()
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "elapsed_ms": int((time.time() - t0) * 1000)}

    pages: dict[tuple[str, int], dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        scope_key = row.get("scope_key")
        if not scope_key:
            continue
        rank = max(1, int(row.get("rank") or 1))
        page_offset = ((rank - 1) // safe_limit) * safe_limit
        page = pages.setdefault(
            (str(scope_key), page_offset),
            {
                "items": [],
                "platform": row.get("platform"),
                "category": (
                    _split_info_compound_value(str(row.get("value") or ""))[0]
                    if row.get("dimension") == "section_subcategory"
                    else row.get("value") if row.get("dimension") in ("category", "section_category") else None
                ),
                "total": int(row.get("total_count") or 0),
                "offset": page_offset,
                "limit": safe_limit,
                "has_more": False,
                "next_offset": None,
                "data_backend": feed_read_backend(),
                "read_model": "info_platforms_v1",
                "scope_dimension": row.get("dimension"),
                "scope_value": row.get("value") or "",
                "overview_generated_at": _timestamp_value(active.get("generated_at")),
                "overview_max_fetched_at": _timestamp_value(active.get("max_fetched_at")),
            },
        )
        item = _item_from_read_model_card(row.get("card_json"))
        if item:
            page["items"].append(item)

    item_count = 0
    for page in pages.values():
        item_count += len(page["items"])
        page_offset = int(page.get("offset") or 0)
        next_offset = page_offset + len(page["items"])
        page["has_more"] = next_offset < int(page.get("total") or 0)
        page["next_offset"] = next_offset if page["has_more"] else None
        dimension = page.get("scope_dimension")
        value = page.get("scope_value") or ""
        if dimension == "section_category":
            cache_key = _info_read_model_section_category_page_cache_key(
                schema=schema,
                category=value,
                offset=page_offset,
                limit=safe_limit,
            )
        elif dimension == "section_subcategory":
            category_value, subcategory_value = _split_info_compound_value(value)
            cache_key = _info_read_model_section_category_page_cache_key(
                schema=schema,
                category=category_value,
                subcategory=subcategory_value,
                offset=page_offset,
                limit=safe_limit,
            )
        else:
            group_value, source_value = _split_info_compound_value(value) if dimension == "group_source" else ("", "")
            cache_key = _info_read_model_page_cache_key(
                schema=schema,
                platform=str(page.get("platform") or ""),
                source=source_value if dimension == "group_source" else value if dimension == "source" else None,
                group=group_value if dimension == "group_source" else value if dimension == "group" else None,
                category=value if dimension == "category" else None,
                offset=page_offset,
                limit=safe_limit,
                exclude_ids=[],
            )
        _cache_set_copy(cache_key, page)
    return {
        "ok": True,
        "pages": len(pages),
        "items": item_count,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def set_status(
    *,
    item_id: str,
    action: str,
    force: bool = False,
    user_id: str | None = None,
    can_access_all: bool = False,
) -> dict[str, Any]:
    col = STATUS_COLUMNS.get(action)
    if col is None:
        raise RemoteDBConfigError(f"Invalid status action: {action!r}")
    if not user_id:
        return {"ok": True, "skipped": "anonymous", "data_backend": status_backend()}

    schema = remote_schema()
    # BE-3(B4): 权限检查合并进同一个连接——原实现独立 connect() 做检查,
    # 最高频写路径每次占 2 个池连接并双付 checkout 开销。
    with connect() as conn:
        with conn.cursor() as cur:
            item_row = cur.execute(
                f"SELECT platform, user_id FROM {schema}.items WHERE id = %(item_id)s",
                {"item_id": item_id},
            ).fetchone()
            if not item_row:
                return {"ok": False, "not_found": True, "data_backend": status_backend()}
            if item_row["platform"] == "manual" and not can_access_all and item_row["user_id"] != user_id:
                return {"ok": False, "not_found": True, "data_backend": status_backend()}
            current = cur.execute(
                f"""SELECT {col}
                      FROM {schema}.item_status
                     WHERE user_id = %(user_id)s AND item_id = %(item_id)s""",
                {"user_id": user_id, "item_id": item_id},
            ).fetchone()
            if action in ("starred", "hidden") and not force and current and current[col]:
                cur.execute(
                    f"""UPDATE {schema}.item_status
                           SET {col} = NULL
                         WHERE user_id = %(user_id)s AND item_id = %(item_id)s""",
                    {"user_id": user_id, "item_id": item_id},
                )
                value = None
            else:
                cur.execute(
                    f"""INSERT INTO {schema}.item_status (user_id, item_id, {col})
                        VALUES (%(user_id)s, %(item_id)s, now())
                        ON CONFLICT (user_id, item_id) DO UPDATE SET {col} = excluded.{col}
                        RETURNING {col}""",
                    {"user_id": user_id, "item_id": item_id},
                )
                row = cur.fetchone()
                value = _timestamp_value(row[col]) if row else None
        conn.commit()
    # Item-status mutation only affects this user's view of feed (read/click/star/hide).
    # Other users' feed caches are unaffected.
    clear_user_cache_keys(user_id)
    return {"ok": True, "item_id": item_id, "action": action, col: value, "data_backend": status_backend()}


def get_stats(
    *,
    user_id: str | None = None,
    public_only: bool = False,
    manual_owner_user_id: str | None = None,
    min_github_stars: int = 50,
) -> dict:
    schema = remote_schema()
    cache_key = (
        "stats_per_platform",
        schema,
        user_id or "",
        bool(public_only),
        manual_owner_user_id or "",
        int(min_github_stars),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    if user_id is None and public_only and not manual_owner_user_id:
        platform_result = query_feed_platforms(
            per_platform=50,
            search=None,
            user_id=None,
            public_only=True,
            manual_owner_user_id=None,
            min_github_stars=min_github_stars,
        )
        result = {
            (platform or "_unknown"): {
                "total": int(total or 0),
                "unread": int(total or 0),
            }
            for platform, total in (platform_result.get("platform_counts") or {}).items()
        }
        return _cache_set_copy(cache_key, result)
    where, params = _base_item_where(
        public_only=public_only,
        manual_owner_user_id=manual_owner_user_id,
        min_github_stars=min_github_stars,
    )
    with connect() as conn:
        _set_short_statement_timeout(conn, 1500)
        status_join, status_params, _ = _item_status_join(schema, user_id)
        params = {**params, **status_params}
        unread_sql = (
            "sum(case when s.clicked_at is null and s.hidden_at is null then 1 else 0 end)"
            if user_id
            else "count(*)"
        )
        rows = conn.execute(
            f"""SELECT i.platform, count(*) AS total,
                       {unread_sql} AS unread
                  FROM {schema}.items i
                  {status_join}
                  {_where_sql(where)}
                 GROUP BY i.platform""",
            params,
        ).fetchall()
    result = {
        (r["platform"] or "_unknown"): {
            "total": int(r["total"] or 0),
            "unread": int(r["unread"] or 0),
        }
        for r in rows
    }
    _cache_set_copy(cache_key, result)
    return result


def status() -> dict:
    schema = remote_schema()
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            """SELECT
                 COALESCE(MAX(CASE WHEN relname = 'items' THEN reltuples END), 0)::bigint AS items,
                 COALESCE(MAX(CASE WHEN relname = 'clusters' THEN reltuples END), 0)::bigint AS clusters,
                 COALESCE(MAX(CASE WHEN relname = 'cluster_items' THEN reltuples END), 0)::bigint AS cluster_items,
                 COALESCE(MAX(CASE WHEN relname = 'fetch_runs' THEN reltuples END), 0)::bigint AS fetch_runs
               FROM pg_class
              WHERE oid IN (
                to_regclass(%s),
                to_regclass(%s),
                to_regclass(%s),
                to_regclass(%s)
              )""",
            (
                f"{schema}.items",
                f"{schema}.clusters",
                f"{schema}.cluster_items",
                f"{schema}.fetch_runs",
            ),
        ).fetchone()
        version = conn.execute("SELECT version() AS version").fetchone()
    return {
        "backend": event_read_backend(),
        "event_backend": event_read_backend(),
        "feed_backend": feed_read_backend(),
        "status_backend": status_backend(),
        "schema": schema,
        "counts": dict(row or {}),
        "postgres_version": (version or {}).get("version"),
    }
