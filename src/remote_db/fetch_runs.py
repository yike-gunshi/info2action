from __future__ import annotations


def pipeline_write_mode() -> str:
    """Return the configured pipeline write mode.

    Phase 2A intentionally keeps the production write path as local SQLite
    followed by an explicit Supabase incremental sync. Full direct Supabase
    writes require a larger per-stage migration of fetch/enrich/cluster/publish
    transactions, so unsupported modes fail fast instead of silently falling
    back to local writes.
    """
    raw = _normalized(_runtime_env().get(PIPELINE_WRITE_MODE_ENV))
    if not raw:
        raw = "supabase_direct" if storage_mode() == STORAGE_REMOTE_ONLY else PIPELINE_SQLITE_THEN_SYNC
    mode = raw.replace("-", "_")
    if mode in {"sqlite", "local", "local_then_sync", PIPELINE_SQLITE_THEN_SYNC}:
        return PIPELINE_SQLITE_THEN_SYNC
    if mode in PIPELINE_UNSUPPORTED_DIRECT:
        return "supabase_direct"
    if mode in PIPELINE_UNSUPPORTED_DUAL:
        return "dual_write"
    raise RemoteDBConfigError(
        f"Invalid {PIPELINE_WRITE_MODE_ENV}: {raw!r}. "
        f"Use '{PIPELINE_SQLITE_THEN_SYNC}' until direct Supabase writes are implemented."
    )


def remote_sync_after_pipeline_enabled() -> bool:
    return _truthy(_runtime_env().get(REMOTE_SYNC_AFTER_PIPELINE_ENV))


def assert_pipeline_write_mode_ready() -> dict[str, Any]:
    mode = pipeline_write_mode()
    if mode == "supabase_direct":
        writers = {
            "fetch": fetch_write_backend(),
            "enrich": enrich_backend(),
            "embedding": embedding_backend(),
            "cluster": cluster_backend(),
            "app_state": app_state_backend(),
        }
        missing = [name for name, backend in writers.items() if backend != "supabase"]
        if missing:
            raise RemoteDBConfigError(
                f"{PIPELINE_WRITE_MODE_ENV}=supabase_direct requires direct Supabase writers "
                f"for {', '.join(missing)}. Set {FETCH_WRITE_BACKEND_ENV}=supabase, "
                f"{ENRICH_BACKEND_ENV}=supabase, {EMBEDDING_BACKEND_ENV}=supabase, "
                f"{CLUSTER_BACKEND_ENV}=supabase, and {APP_STATE_BACKEND_ENV}=supabase."
            )
        return {
            "mode": mode,
            "remote_sync_after_pipeline": False,
            "direct_writers": writers,
        }
    if mode != PIPELINE_SQLITE_THEN_SYNC:
        raise RemoteDBConfigError(
            f"{PIPELINE_WRITE_MODE_ENV}={mode} is not implemented yet. "
            f"Use {PIPELINE_WRITE_MODE_ENV}={PIPELINE_SQLITE_THEN_SYNC} together with "
            f"{REMOTE_SYNC_AFTER_PIPELINE_ENV}=1 for the current production pipeline."
        )
    return {
        "mode": mode,
        "remote_sync_after_pipeline": remote_sync_after_pipeline_enabled(),
    }


def _remote_pending_scan_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_PENDING_SCAN_TIMEOUT_MS_ENV, 30000, min_value=5000)


def set_pending_scan_statement_timeout(conn: Any) -> None:
    _set_short_statement_timeout(conn, _remote_pending_scan_timeout_ms())


def _mark_stale_payload(payload: Any, *, source: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    result = copy.deepcopy(payload)
    result["degraded"] = True
    result["stale"] = True
    result["stale_source"] = source
    return result


def _write_feed_snapshot_async(schema: str, snapshot_key: str, payload: Any) -> None:
    if _remote_snapshot_ttl() <= 0:
        return
    with _SNAPSHOT_WRITE_LOCK:
        if snapshot_key in _SNAPSHOT_WRITES_IN_FLIGHT:
            return
        _SNAPSHOT_WRITES_IN_FLIGHT.add(snapshot_key)

    payload_copy = copy.deepcopy(payload)

    def _worker() -> None:
        try:
            try:
                with connect() as conn:
                    _write_feed_snapshot(conn, schema, snapshot_key, payload_copy)
            except Exception:
                pass
        finally:
            with _SNAPSHOT_WRITE_LOCK:
                _SNAPSHOT_WRITES_IN_FLIGHT.discard(snapshot_key)

    threading.Thread(target=_worker, name=f"feed-snapshot:{snapshot_key[:32]}", daemon=True).start()


def start_fetch_run_remote(pg_conn: Any | None = None) -> int:
    """Create a fetch run in Supabase and return its id."""
    if pg_conn is not None:
        _ensure_remote_id_sequence(pg_conn, "fetch_runs")
        row = pg_conn.execute(
            f"INSERT INTO {remote_schema()}.fetch_runs (started_at, status) "
            "VALUES (%s, %s) RETURNING id",
            (datetime.now(timezone.utc), "running"),
        ).fetchone()
        _commit_if_supported(pg_conn)
        return int(row["id"] if isinstance(row, dict) else row[0])

    with connect() as conn:
        return start_fetch_run_remote(conn)


def fetch_run_heartbeat_grace_seconds() -> int:
    return _env_int(
        _runtime_env(),
        FETCH_RUN_HEARTBEAT_GRACE_SEC_ENV,
        600,
        min_value=60,
    )


def fetch_run_runtime_stale_grace_seconds() -> int:
    """运行时判活/孤儿回收的心跳容忍窗口(见 env 常量注释)。

    下限钉在基础 grace 之上,保证运行时窗口不会比重启恢复窗口更激进。
    """
    base = fetch_run_heartbeat_grace_seconds()
    return _env_int(
        _runtime_env(),
        FETCH_RUN_RUNTIME_STALE_GRACE_SEC_ENV,
        max(1800, base),
        min_value=base,
    )


def touch_fetch_run_heartbeat_remote(
    pg_conn: Any | None = None,
    *,
    run_id: int,
    owner: str,
    touched_at: datetime | None = None,
) -> None:
    """Refresh the remote fetch-run lease for a live backend process."""
    if pg_conn is None:
        with connect() as conn:
            touch_fetch_run_heartbeat_remote(
                conn,
                run_id=run_id,
                owner=owner,
                touched_at=touched_at,
            )
            return

    heartbeat_at = touched_at or datetime.now(timezone.utc)
    payload = {
        "_heartbeat_at": heartbeat_at.isoformat(),
        "_heartbeat_owner": str(owner or "")[:160],
    }
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET stats_json = COALESCE(stats_json, '{{}}'::jsonb) || %s
             WHERE id = %s
               AND status = 'running'""",
        (_maybe_jsonb(payload), int(run_id)),
    )
    _commit_if_supported(pg_conn)


def finish_fetch_run_remote(
    pg_conn: Any | None,
    run_id: int,
    stats: dict[str, Any] | Any,
    error: str | None = None,
) -> None:
    """Mark a Supabase fetch run complete."""
    if pg_conn is None:
        with connect() as conn:
            finish_fetch_run_remote(conn, run_id, stats, error)
            return

    stats_payload = dict(stats or {}) if isinstance(stats, dict) else {"value": stats}
    finished_at = datetime.now(timezone.utc)
    try:
        stats_payload["_audit"] = build_fetch_run_audit_summary_remote(
            pg_conn,
            run_id,
            stats_payload,
            finished_at=finished_at,
        )
    except Exception as exc:
        _rollback_safely(pg_conn)
        stats_payload["_audit_error"] = str(exc)[:200]
        print(
            f"[remote-db] failed to build fetch-run audit snapshot for run {run_id}: {exc}",
            flush=True,
        )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET finished_at = %s,
                   status = %s,
                   stats_json = %s,
                   error_msg = %s
             WHERE id = %s""",
        (
            finished_at,
            "error" if error else "done",
            _maybe_jsonb(stats_payload),
            error,
            run_id,
        ),
    )
    _commit_if_supported(pg_conn)


def mark_orphaned_fetch_runs_remote(
    pg_conn: Any | None = None,
    *,
    started_before: datetime,
    heartbeat_stale_before: datetime | None = None,
    reason: str,
    limit: int = 20,
) -> list[int]:
    """Mark running remote fetch runs from a previous backend process as interrupted."""
    if pg_conn is None:
        with connect() as conn:
            return mark_orphaned_fetch_runs_remote(
                conn,
                started_before=started_before,
                heartbeat_stale_before=heartbeat_stale_before,
                reason=reason,
                limit=limit,
            )

    finished_at = datetime.now(timezone.utc)
    stale_before = heartbeat_stale_before or (
        finished_at - timedelta(seconds=fetch_run_heartbeat_grace_seconds())
    )
    stats_payload = {
        "_result_status": "interrupted",
        "_interrupted_at": finished_at.isoformat(),
        "_interrupted_reason": reason,
        "_orphaned_fetch_recovery": True,
    }
    rows = pg_conn.execute(
        f"""WITH orphaned AS (
                SELECT id
                  FROM {remote_schema()}.fetch_runs
                 WHERE status = 'running'
                   AND started_at < %s
                   AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at) < %s
                 ORDER BY started_at ASC
                 LIMIT %s
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE {remote_schema()}.fetch_runs fr
               SET finished_at = %s,
                   status = %s,
                   error_msg = %s,
                   stats_json = COALESCE(fr.stats_json, '{{}}'::jsonb) || %s
              FROM orphaned
             WHERE fr.id = orphaned.id
             RETURNING fr.id""",
        (
            started_before,
            stale_before,
            max(1, int(limit or 20)),
            finished_at,
            "error",
            reason,
            _maybe_jsonb(stats_payload),
        ),
    ).fetchall()
    _commit_if_supported(pg_conn)
    return [int(row["id"] if isinstance(row, dict) else row[0]) for row in rows]


def mark_fetch_runs_interrupted_remote(
    pg_conn: Any | None = None,
    *,
    run_ids: list[int] | tuple[int, ...],
    reason: str,
) -> list[int]:
    """Mark known in-process remote fetch runs as interrupted."""
    normalized_run_ids = sorted({int(run_id) for run_id in run_ids if run_id is not None})
    if not normalized_run_ids:
        return []
    if pg_conn is None:
        with connect() as conn:
            return mark_fetch_runs_interrupted_remote(
                conn,
                run_ids=normalized_run_ids,
                reason=reason,
            )

    finished_at = datetime.now(timezone.utc)
    stats_payload = {
        "_result_status": "interrupted",
        "_interrupted_at": finished_at.isoformat(),
        "_interrupted_reason": reason,
        "_shutdown_interruption": True,
    }
    rows = pg_conn.execute(
        f"""UPDATE {remote_schema()}.fetch_runs
               SET finished_at = %s,
                   status = %s,
                   error_msg = %s,
                   stats_json = COALESCE(stats_json, '{{}}'::jsonb) || %s
             WHERE status = 'running'
               AND id = ANY(%s)
             RETURNING id""",
        (
            finished_at,
            "error",
            reason,
            _maybe_jsonb(stats_payload),
            normalized_run_ids,
        ),
    ).fetchall()
    _commit_if_supported(pg_conn)
    return [int(row["id"] if isinstance(row, dict) else row[0]) for row in rows]


def _fetch_run_to_audit_remote(conn: Any, row: Any, *, build_missing_audit: bool = True) -> dict[str, Any]:
    data = dict(row)
    stats = _json_value(data.get("stats_json"))
    stats = stats if isinstance(stats, dict) else {}
    audit = stats.get("_audit") if isinstance(stats, dict) else None
    if not isinstance(audit, dict):
        stage_durations = stats.get("_stage_durations_sec") or stats.get("stage_durations_sec") or {}
        total_new_items = _optional_int(data.get("total_new_items"))
        if total_new_items is None:
            total_new_items = _optional_int(stats.get("_new_items_count"))
        ai_summarized = _optional_int(data.get("ai_summarized"))
        if ai_summarized is None:
            ai_summarized = _optional_int(stats.get("_ai_summarized"))
        ai_failed = _optional_int(data.get("ai_failed"))
        if ai_failed is None:
            ai_failed = _optional_int(stats.get("_ai_failed"))
        clustered_items = _optional_int(data.get("clustered_items"))
        if clustered_items is None:
            clustered_items = _optional_int(stats.get("_clustered_items"))
        touched_clusters = _optional_int(data.get("touched_clusters"))
        if touched_clusters is None:
            touched_clusters = _optional_int(stats.get("_touched_clusters"))
        published_clusters = _optional_int(data.get("published_clusters"))
        if published_clusters is None:
            published_clusters = _optional_int(stats.get("_published_clusters_count"))
        audit = (
            build_fetch_run_audit_summary_remote(conn, int(data["id"]), stats)
            if build_missing_audit
            else {
                "version": "v15.2",
                "run_id": int(data["id"]),
                "new_items_count": total_new_items,
                "stage_durations_sec": stage_durations if isinstance(stage_durations, dict) else {},
                "result_status": stats.get("_result_status"),
                "platform_counts": [],
                "platform_source_counts": [],
                "pill_counts": [],
                "ai_summary": {
                    "summarized": ai_summarized,
                    "failed": ai_failed,
                    "pending": (
                        max(0, total_new_items - (ai_summarized or 0) - (ai_failed or 0))
                        if (
                            total_new_items is not None
                            and (ai_summarized is not None or ai_failed is not None)
                        )
                        else None
                    ),
                },
                "event_cluster": {
                    "clustered_items": clustered_items,
                    "touched_clusters": touched_clusters,
                    "published_clusters": published_clusters,
                },
                "errors": _extract_fetch_errors(stats),
            }
        )
    data["started_at"] = _timestamp_value(data.get("started_at"))
    data["finished_at"] = _timestamp_value(data.get("finished_at"))
    data["stats"] = stats
    data["audit"] = audit
    data["duration_sec"] = audit.get("duration_sec") or _elapsed_seconds(
        data.get("started_at"),
        data.get("finished_at"),
    )
    data["total_new_items"] = audit.get("new_items_count")
    data.pop("stats_json", None)
    return data


def list_fetch_run_audits_remote(
    limit: int = 50,
    offset: int = 0,
    *,
    pg_conn: Any | None = None,
    build_missing_audit: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    offset = max(0, int(offset or 0))
    cache_key = (
        "admin_fetch_runs_result",
        remote_schema(),
        limit,
        offset,
        bool(build_missing_audit),
    )
    if pg_conn is None:
        cached = _cache_get_copy(cache_key)
        if cached is not None:
            return cached

        def _compute() -> list[dict[str, Any]]:
            cached_inside = _cache_get_copy(cache_key)
            if cached_inside is not None:
                return cached_inside
            with connect() as conn:
                result = _list_fetch_run_audits_remote_uncached(
                    conn,
                    limit=limit,
                    offset=offset,
                    build_missing_audit=build_missing_audit,
                )
            return _cache_set_copy(cache_key, result)

        return _singleflight_sync(cache_key, _compute)

    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        return _list_fetch_run_audits_remote_uncached(
            conn,
            limit=limit,
            offset=offset,
            build_missing_audit=build_missing_audit,
        )
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def _list_fetch_run_audits_remote_uncached(
    conn: Any,
    *,
    limit: int,
    offset: int,
    build_missing_audit: bool = False,
) -> list[dict[str, Any]]:
    schema = remote_schema()
    rows = conn.execute(
        f"""SELECT id, started_at, finished_at, status, stats_json, error_msg
              FROM {schema}.fetch_runs
             ORDER BY id DESC
             LIMIT %s OFFSET %s""",
        (limit, offset),
    ).fetchall()
    if not rows:
        return []

    return [
        _fetch_run_to_audit_remote(conn, row, build_missing_audit=build_missing_audit)
        for row in rows
    ]


def get_fetch_run_audit_remote(run_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.fetch_runs WHERE id = %s",
            (run_id,),
        ).fetchone()
        return _fetch_run_to_audit_remote(conn, row) if row else None


def query_fetch_run_audit_items_remote(
    run_id: int,
    *,
    platform: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or 50), 100))
    bounded_offset = max(0, int(offset or 0))
    with connect() as conn:
        if not conn.execute(
            f"SELECT 1 FROM {remote_schema()}.fetch_runs WHERE id = %s",
            (run_id,),
        ).fetchone():
            return {"missing_run": True}
        items_sql, item_params, source_kind = _new_run_items_sql_remote(conn, run_id)
        where = []
        params = dict(item_params)
        if platform:
            where.append("ni.platform = %(platform)s")
            params["platform"] = platform
        if source:
            where.append("ni.source = %(source)s")
            params["source"] = source
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM ({items_sql}) ni {where_sql}",
            params,
        ).fetchone()
        rows = conn.execute(
            f"""SELECT ni.id, ni.title, ni.platform, ni.source, ni.url,
                       ni.ai_summary, ni.ai_error_count, ni.ai_last_error,
                       ni.ai_category, ni.ai_categories, ni.cluster_id,
                       ni.created_at, ni.fetched_at
                  FROM ({items_sql}) ni
                  {where_sql}
                 ORDER BY ni.created_at DESC NULLS LAST, ni.id DESC
                 LIMIT %(limit)s OFFSET %(offset)s""",
            {**params, "limit": bounded_limit, "offset": bounded_offset},
        ).fetchall()
    items = []
    for raw in rows:
        item = dict(raw)
        item["created_at"] = _timestamp_value(item.get("created_at"))
        item["fetched_at"] = _timestamp_value(item.get("fetched_at"))
        item["pill"] = _remote_pill_from_item(item)
        item["ai_status"] = "failed" if (item.get("ai_error_count") or 0) > 0 or item.get("ai_last_error") else (
            "summarized" if item.get("ai_summary") else "pending"
        )
        item["cluster_status"] = "clustered" if item.get("cluster_id") is not None else "pending"
        items.append(item)
    return {
        "items": items,
        "total": int(total["count"] if total else 0),
        "source": source_kind,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


def _admin_console_pipeline_signal(
    latest_run: dict[str, Any] | None,
    counts: dict[str, Any] | None,
    now_utc: datetime,
) -> dict[str, Any]:
    signal = {
        "key": "pipeline",
        "level": "unknown",
        "label": "抓取 Pipeline",
        "detail": "无任何 run 记录",
        "link": "runs",
    }
    if not latest_run:
        return signal

    status_text = str(latest_run.get("status") or "unknown").lower()
    success_24h = int((counts or {}).get("success_runs_24h") or 0)
    success_48h = int((counts or {}).get("success_runs_48h") or 0)
    total_24h = int((counts or {}).get("total_runs_24h") or 0)
    run_time = latest_run.get("finished_at") or latest_run.get("started_at")
    detail = f"run #{latest_run.get('id')} {status_text} · {_admin_console_age_text(run_time, now_utc)}"

    if status_text in {"failed", "error"} or status_text.startswith("failed") or success_48h == 0:
        level = "crit"
    elif "partial" in status_text or total_24h == 0 or success_24h == 0:
        level = "warn"
    elif status_text == "success" and success_24h > 0:
        level = "ok"
    else:
        level = "warn"
    signal.update({"level": level, "detail": detail})
    return signal


def get_last_fetch_remote() -> dict[str, Any] | None:
    """Return the most recent remote fetch run in the same shape as db.get_last_fetch."""
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT id, started_at, finished_at, status, stats_json, error_msg
                  FROM {remote_schema()}.fetch_runs
                 ORDER BY id DESC
                 LIMIT 1"""
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["started_at"] = _timestamp_value(item.get("started_at"))
    item["finished_at"] = _timestamp_value(item.get("finished_at"))
    item["stats_json"] = _json_value(item.get("stats_json"))
    return item


def has_recent_running_fetch_remote(max_age_minutes: int | None = None) -> bool:
    """Return whether Supabase has a recent in-flight fetch run.

    Liveness is decided by heartbeat freshness alone: a row is "running" iff its
    status is ``running`` and its heartbeat (falling back to ``started_at`` for
    runs that never wrote one) is within the runtime-stale grace window. Zombie
    rows from old failed finish paths have a stale heartbeat and are excluded.

    稳定性加固(2026-07-10 BF-0710-fetch-guards): 移除了旧的 ``started_at >= now()
    - 180min`` 硬龄窗口——它与心跳判活冗余,且比"各阶段超时之和(~3.2h)"还短,
    导致一次合法长 run 跑过 3h 后被守卫误判成"无 run 在跑"→放行第二条 pipeline→
    双 pipeline 打爆 Supabase Micro。心跳新鲜即视为在跑,与孤儿回收的"心跳陈旧"
    互为反面,不再耦合 run 时长。若运维确需一个上限窗口,显式传 ``max_age_minutes``
    或设 ``INFO2ACTION_REMOTE_RUNNING_FETCH_MAX_AGE_MINUTES``(默认不启用)。

    Scheduler callers should fail closed when this query cannot be answered.
    """
    age_minutes = max_age_minutes
    if age_minutes is None:
        raw = (_runtime_env().get(REMOTE_RUNNING_FETCH_MAX_AGE_MIN_ENV) or "").strip()
        if raw:
            try:
                age_minutes = max(1, int(raw))
            except (ValueError, TypeError):
                age_minutes = None
    age_clause = ""
    params: list[Any] = []
    if age_minutes is not None:
        age_clause = "AND started_at >= now() - (%s::int * interval '1 minute')"
        params.append(int(age_minutes))
    params.append(fetch_run_runtime_stale_grace_seconds())
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT EXISTS (
                    SELECT 1
                      FROM {remote_schema()}.fetch_runs
                     WHERE status = 'running'
                       {age_clause}
                       AND COALESCE(NULLIF(stats_json->>'_heartbeat_at', '')::timestamptz, started_at)
                           >= now() - (%s::int * interval '1 second')
                     LIMIT 1
                 ) AS has_running""",
            tuple(params),
        ).fetchone()
    if isinstance(row, dict):
        return bool(row.get("has_running"))
    try:
        return bool(row[0])
    except Exception:
        return False


def has_recent_finished_fetch_remote(*, minutes: int = 30) -> bool:
    safe_minutes = max(1, int(minutes or 30))
    with connect() as conn:
        _set_short_statement_timeout(conn)
        row = conn.execute(
            f"""SELECT EXISTS (
                    SELECT 1
                      FROM {remote_schema()}.fetch_runs
                     WHERE finished_at IS NOT NULL
                       AND finished_at >= now() - (%s::int * interval '1 minute')
                     LIMIT 1
                 ) AS has_finished""",
            (safe_minutes,),
        ).fetchone()
    return bool(_row_get(row, "has_finished", False))


def _fetch_run_item_upsert_sql(schema: str, *, row_count: int = 1) -> str:
    placeholders = _multirow_values_placeholder(5, row_count)
    return f"""INSERT INTO {schema}.fetch_run_items
                  (run_id, item_id, platform, source, was_inserted)
                VALUES {placeholders}
                ON CONFLICT (run_id, item_id) DO UPDATE SET
                  platform = excluded.platform,
                  source = excluded.source,
                  was_inserted = CASE
                    WHEN {schema}.fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1
                    THEN 1 ELSE 0 END
                WHERE {schema}.fetch_run_items.platform IS DISTINCT FROM excluded.platform
                   OR {schema}.fetch_run_items.source IS DISTINCT FROM excluded.source
                   OR {schema}.fetch_run_items.was_inserted IS DISTINCT FROM CASE
                        WHEN {schema}.fetch_run_items.was_inserted = 1 OR excluded.was_inserted = 1
                        THEN 1 ELSE 0 END"""


def publish_run_remote(pg_conn: Any | None, run_id: int) -> int:
    if pg_conn is None:
        with connect() as conn:
            return publish_run_remote(conn, run_id)
    schema = remote_schema()
    batch_size = _env_int(
        _runtime_env(),
        "INFO2ACTION_PUBLISH_RUN_BATCH_SIZE",
        25,
        min_value=1,
    )
    published = 0
    while True:
        rows = pg_conn.execute(
            f"""SELECT id, COALESCE(live_version, 0) + 1 AS new_version
                  FROM {schema}.clusters
                 WHERE last_touched_run_id = %s
                   AND (
                     ai_title_draft IS NOT NULL
                     OR ai_summary_draft IS NOT NULL
                     OR ai_key_points_draft IS NOT NULL
                     OR pending_is_visible_in_feed IS NOT NULL
                   )
                 ORDER BY id ASC
                 LIMIT %s""",
            (run_id, batch_size),
        ).fetchall()
        if not rows:
            break

        values: list[tuple[int, int]] = [
            (int(_row_get(row, "id")), int(_row_get(row, "new_version")))
            for row in rows
        ]
        placeholders = ", ".join(["(%s, %s)"] * len(values))
        value_params: list[int] = [item for pair in values for item in pair]
        now = datetime.now(timezone.utc)
        updated = pg_conn.execute(
            f"""WITH publish_values(id, new_version) AS (
                    VALUES {placeholders}
                 )
                UPDATE {schema}.clusters c
                   SET ai_title = COALESCE(c.ai_title_draft, c.ai_title),
                       ai_summary = COALESCE(c.ai_summary_draft, c.ai_summary),
                       ai_key_points = COALESCE(c.ai_key_points_draft, c.ai_key_points),
                       ai_title_draft = NULL,
                       ai_summary_draft = NULL,
                       ai_key_points_draft = NULL,
                       is_visible_in_feed = COALESCE((c.pending_is_visible_in_feed <> 0), c.is_visible_in_feed),
                       last_summary_warnings_json = COALESCE(c.pending_summary_warnings_json, c.last_summary_warnings_json),
                       pending_is_visible_in_feed = NULL,
                       pending_summary_warnings_json = NULL,
                       live_version = v.new_version,
                       last_updated_at = %s,
                       published_at = %s,
                       published_run_id = %s
                  FROM publish_values v
                 WHERE c.id = v.id
             RETURNING c.id, v.new_version""",
            (*value_params, now, now, run_id),
        ).fetchall()
        if not updated:
            _commit_if_supported(pg_conn)
            break

        action_values = [
            (int(_row_get(row, "id")), int(_row_get(row, "new_version")))
            for row in updated
        ]
        action_placeholders = ", ".join(["(%s, %s)"] * len(action_values))
        action_params: list[int] = [item for pair in action_values for item in pair]
        pg_conn.execute(
            f"""UPDATE {schema}.actions a
                   SET is_stale = 1
                  FROM (VALUES {action_placeholders}) AS v(id, new_version)
                 WHERE a.source_type = 'cluster'
                   AND a.source_id = v.id::text
                   AND (a.cluster_version IS NULL OR a.cluster_version < v.new_version)
                   AND a.is_stale = 0""",
            tuple(action_params),
        )
        published += len(updated)
        _commit_if_supported(pg_conn)
    return published
