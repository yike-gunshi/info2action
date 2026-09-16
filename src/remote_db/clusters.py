from __future__ import annotations


def embedding_to_remote() -> bool:
    return embedding_backend() == "supabase"


def cluster_to_remote() -> bool:
    return cluster_backend() == "supabase"


def _cluster_bundle_cache_ttl_sec(env: dict[str, str] | None = None) -> int:
    # 事件弹窗 bundle 进程缓存 TTL(可配置,默认 300s,原隐含 180s)。
    # 事件详情/来源列表变化缓慢,适度延长减少重复打开与压力期冷读的 DB 开销。
    return _env_int(
        env or _runtime_env(),
        "INFO2ACTION_CLUSTER_BUNDLE_CACHE_TTL_SEC",
        300,
        min_value=0,
    )


def _remote_cluster_write_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_CLUSTER_WRITE_TIMEOUT_MS_ENV, 300000, min_value=30000)


def set_cluster_write_statement_timeout(conn: Any) -> None:
    _set_short_statement_timeout(conn, _remote_cluster_write_timeout_ms())


def build_fetch_run_audit_summary_remote(
    conn: Any,
    run_id: int,
    raw_stats: dict[str, Any] | None = None,
    finished_at: Any = None,
) -> dict[str, Any]:
    schema = remote_schema()
    run = conn.execute(
        f"SELECT * FROM {schema}.fetch_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if not run:
        return {}
    run_data = dict(run)
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    if _run_has_item_records_remote(conn, run_id):
        source = "fetch_run_items"
        item_rows = conn.execute(
            f"""SELECT i.platform, i.source, i.ai_summary, i.ai_error_count, i.ai_last_error,
                       i.cluster_id, i.ai_categories, i.ai_category
                  FROM {schema}.fetch_run_items fri
                  JOIN {schema}.items i ON i.id = fri.item_id
                 WHERE fri.run_id = %s
                   AND fri.was_inserted = 1""",
            (run_id,),
        ).fetchall()
    else:
        source = "created_at_fallback"
        item_rows = conn.execute(
            f"""SELECT i.platform, i.source, i.ai_summary, i.ai_error_count, i.ai_last_error,
                       i.cluster_id, i.ai_categories, i.ai_category
                  FROM {schema}.items i
                  JOIN {schema}.fetch_runs r ON r.id = %s
                 WHERE i.fetch_run_id = r.id
                   AND i.created_at >= r.started_at
                   AND i.created_at <= coalesce(r.finished_at, now())""",
            (run_id,),
        ).fetchall()
    item_rows = [dict(r) for r in item_rows]

    platform_source_counts: dict[tuple[str, str], int] = {}
    platform_counts: dict[str, int] = {}
    pill_count_map: dict[str, int] = {}
    summarized = 0
    ai_failed = 0
    clustered_items = 0
    touched_cluster_ids: set[Any] = set()

    for row in item_rows:
        platform = row.get("platform") or "unknown"
        source_name = row.get("source") or "unknown"
        platform_source_counts[(platform, source_name)] = platform_source_counts.get((platform, source_name), 0) + 1
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

        if row.get("ai_summary"):
            summarized += 1
        if int(row.get("ai_error_count") or 0) > 0 or row.get("ai_last_error") is not None:
            ai_failed += 1
        cluster_id = row.get("cluster_id")
        if cluster_id is not None:
            clustered_items += 1
            touched_cluster_ids.add(cluster_id)
        pill = _remote_pill_from_item(row)
        pill_count_map[pill] = pill_count_map.get(pill, 0) + 1

    platform_source = [
        {"platform": platform, "source": source_name, "count": count}
        for (platform, source_name), count in sorted(
            platform_source_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    ]
    pill_counts = [
        {"pill": pill, "count": count}
        for pill, count in sorted(pill_count_map.items(), key=lambda item: (-item[1], item[0]))
    ]

    ended_at = finished_at or run_data.get("finished_at")
    stage_durations = raw_stats.get("_stage_durations_sec") or raw_stats.get("stage_durations_sec") or {}
    result_status = raw_stats.get("_result_status")
    total_new = len(item_rows)
    touched_clusters = len(touched_cluster_ids)
    published_clusters = _optional_int(raw_stats.get("_published_clusters_count"))
    if published_clusters is None:
        published_row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {schema}.clusters WHERE published_run_id = %s",
            (run_id,),
        ).fetchone()
        published_clusters = int((published_row or {}).get("count") or 0)
    return {
        "version": "v15.2",
        "run_id": run_id,
        "source": source,
        "duration_sec": _elapsed_seconds(run_data.get("started_at"), ended_at),
        "stage_durations_sec": stage_durations if isinstance(stage_durations, dict) else {},
        "result_status": result_status,
        "new_items_count": total_new,
        "platform_counts": [
            {"platform": key, "count": value}
            for key, value in sorted(platform_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "platform_source_counts": platform_source,
        "pill_counts": pill_counts,
        "ai_summary": {
            "summarized": summarized,
            "failed": ai_failed,
            "pending": max(0, total_new - summarized - ai_failed),
        },
        "event_cluster": {
            "clustered_items": clustered_items,
            "touched_clusters": touched_clusters,
            "published_clusters": published_clusters,
        },
        "errors": _extract_fetch_errors(raw_stats),
    }


def _embedding_usage_where_remote(hours: float | None = 24, run_id: int | None = None) -> tuple[str, dict[str, Any]]:
    clauses = []
    params: dict[str, Any] = {}
    if hours is not None:
        try:
            hours_float = float(hours)
        except (TypeError, ValueError):
            hours_float = 24.0
        if hours_float > 0:
            params["since"] = datetime.now(timezone.utc) - timedelta(hours=hours_float)
            clauses.append("created_at >= %(since)s")
    if run_id is not None:
        params["run_id"] = int(run_id)
        clauses.append("run_id = %(run_id)s")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _normalize_embedding_usage_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    if "created_at" in data:
        data["created_at"] = _timestamp_value(data.get("created_at"))
    if "item_ids_json" in data:
        data["item_ids_json"] = _json_value(data.get("item_ids_json"))
    return data


def record_embedding_usage_remote(log: dict[str, Any], pg_conn: Any | None = None) -> int | None:
    """Persist one embedding provider call in the remote audit table."""
    if not isinstance(log, dict):
        return None

    payload = dict(log)
    payload.setdefault("created_at", datetime.now(timezone.utc))
    item_ids = payload.get("item_ids_json")
    if isinstance(item_ids, tuple):
        item_ids = list(item_ids)
    if isinstance(item_ids, str):
        item_ids = _json_value(item_ids)
    payload["item_ids_json"] = _maybe_jsonb(item_ids) if item_ids is not None else None

    columns = (
        "created_at",
        "provider",
        "model",
        "mode",
        "source",
        "stage",
        "run_id",
        "caller_file",
        "caller_func",
        "input_count",
        "input_chars",
        "input_bytes",
        "estimated_tokens",
        "token_estimator",
        "output_count",
        "output_dim",
        "status",
        "error",
        "latency_ms",
        "price_yuan_per_1k_tokens",
        "estimated_cost_yuan",
        "item_ids_json",
    )
    values = [payload.get(col) for col in columns]

    def _insert(conn: Any) -> int | None:
        row = conn.execute(
            f"""INSERT INTO {remote_schema()}.embedding_usage_logs ({','.join(columns)})
                VALUES ({','.join(['%s'] * len(columns))})
                RETURNING id""",
            values,
        ).fetchone()
        conn.commit()
        if row is None:
            return None
        return int(row["id"] if hasattr(row, "keys") else row[0])

    if pg_conn is not None:
        return _insert(pg_conn)
    try:
        with connect() as conn:
            return _insert(conn)
    except Exception:
        rest_id = _record_embedding_usage_remote_rest(payload)
        if rest_id is not None:
            return rest_id
        raise


def _record_embedding_usage_remote_rest(payload: dict[str, Any]) -> int | None:
    """Best-effort Supabase REST fallback when the Postgres pooler is saturated."""
    try:
        url = f"{supabase_project_url()}/rest/v1/embedding_usage_logs"
        key = supabase_service_role_key()
        body = {}
        for key_name, value in payload.items():
            if value is None:
                body[key_name] = None
            elif key_name == "item_ids_json":
                body[key_name] = _json_value(getattr(value, "obj", value))
            elif isinstance(value, datetime):
                body[key_name] = value.isoformat()
            else:
                body[key_name] = value
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Content-Profile": remote_schema(),
                "Prefer": "return=representation",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        rows = json.loads(raw) if raw else []
        if isinstance(rows, list) and rows:
            row = rows[0]
            if isinstance(row, dict) and row.get("id") is not None:
                return int(row["id"])
    except Exception:
        return None
    return None


def get_embedding_usage_audit_remote(
    *,
    hours: float = 24,
    run_id: int | None = None,
    limit: int = 100,
    pg_conn: Any | None = None,
) -> dict[str, Any]:
    schema = remote_schema()
    where, params = _embedding_usage_where_remote(hours=hours, run_id=run_id)
    bounded_limit = max(1, min(int(limit or 100), 500))
    cache_key = (
        "admin_embedding_usage_result",
        schema,
        float(hours or 24),
        run_id,
        bounded_limit,
    )
    if pg_conn is None:
        cached = _cache_get_copy(cache_key)
        if cached is not None:
            return cached

        def _compute() -> dict[str, Any]:
            cached_inside = _cache_get_copy(cache_key)
            if cached_inside is not None:
                return cached_inside
            result = get_embedding_usage_audit_remote(
                hours=hours,
                run_id=run_id,
                limit=bounded_limit,
                pg_conn=False,
            )
            return _cache_set_copy(cache_key, result)

        return _singleflight_sync(cache_key, _compute)

    conn_cm = None
    if pg_conn is None or pg_conn is False:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        summary = conn.execute(
            f"""SELECT COUNT(*) AS total_calls,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                       COALESCE(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END), 0) AS failed_calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(input_chars), 0) AS input_chars,
                       COALESCE(SUM(input_bytes), 0) AS input_bytes,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens_attempted,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_tokens ELSE 0 END), 0) AS estimated_tokens_success,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN estimated_cost_yuan ELSE 0 END), 0.0) AS estimated_cost_yuan_success,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan_all
                  FROM {schema}.embedding_usage_logs
                  {where}""",
            params,
        ).fetchone()
        by_source = conn.execute(
            f"""SELECT COALESCE(source, 'unknown') AS source,
                       COALESCE(stage, '') AS stage,
                       provider,
                       model,
                       status,
                       COUNT(*) AS calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(input_chars), 0) AS input_chars,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
                  FROM {schema}.embedding_usage_logs
                  {where}
                 GROUP BY source, stage, provider, model, status
                 ORDER BY estimated_tokens DESC, calls DESC
                 LIMIT 50""",
            params,
        ).fetchall()
        by_run = conn.execute(
            f"""SELECT run_id,
                       COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS success_calls,
                       COALESCE(SUM(input_count), 0) AS input_count,
                       COALESCE(SUM(estimated_tokens), 0) AS estimated_tokens,
                       COALESCE(SUM(output_count), 0) AS output_count,
                       COALESCE(SUM(estimated_cost_yuan), 0.0) AS estimated_cost_yuan
                  FROM {schema}.embedding_usage_logs
                  {where}
                 GROUP BY run_id
                 ORDER BY MAX(created_at) DESC
                 LIMIT 50""",
            params,
        ).fetchall()
        rows = conn.execute(
            f"""SELECT *
                  FROM {schema}.embedding_usage_logs
                  {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT %(limit)s""",
            {**params, "limit": bounded_limit},
        ).fetchall()
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)
    return {
        "hours": hours,
        "run_id": run_id,
        "summary": dict(summary) if summary else {},
        "by_source": [dict(r) for r in by_source],
        "by_run": [dict(r) for r in by_run],
        "logs": [_normalize_embedding_usage_row(r) for r in rows],
        "limit": bounded_limit,
    }


def _admin_console_embedding_signal(total_calls: int | None, failed_calls: int | None) -> dict[str, Any]:
    signal = {
        "key": "embedding",
        "level": "unknown",
        "label": "Embedding",
        "detail": "24h 无 embedding 调用",
        "link": "runs",
    }
    if total_calls is None:
        signal["detail"] = "embedding 数据不可用"
        return signal
    if total_calls <= 0:
        return signal
    failed = int(failed_calls or 0)
    failure_rate = failed / total_calls
    if failure_rate == 0:
        level = "ok"
    elif failure_rate < 0.10:
        level = "warn"
    else:
        level = "crit"
    signal.update({
        "level": level,
        "detail": f"24h 失败率 {_admin_console_percent(failure_rate)}（{total_calls} 次）",
    })
    return signal


def admin_console_summary_remote(*, now: datetime | None = None) -> dict[str, Any]:
    now_shanghai = _admin_console_now(now)
    now_utc = now_shanghai.astimezone(timezone.utc)
    today = now_shanghai.date().isoformat()
    start_14d = (now_shanghai.date() - timedelta(days=13)).isoformat()
    start_7d = (now_shanghai.date() - timedelta(days=6)).isoformat()
    since_1d = now_utc - timedelta(days=1)
    since_7d = now_utc - timedelta(days=7)
    since_24h = now_utc - timedelta(hours=24)
    since_48h = now_utc - timedelta(hours=48)

    c_metrics = {
        "total_users": None,
        "new_users_today": None,
        "new_users_7d": None,
        "active_users_1d": None,
        "active_users_7d": None,
        "info_click_users_7d": None,
        "info_click_items_7d": None,
        "info_click_items_total": None,
        "highlight_click_users_7d": None,
        "highlight_click_events_7d": None,
        "highlight_click_events_total": None,
    }
    interactions_detail = {
        "starred_users": None,
        "starred_total": None,
        "read_users_7d": None,
        "read_items_7d": None,
        "latest_signup": None,
    }
    cost = {
        "embedding_cost_yuan_24h": None,
        "embedding_calls_24h": None,
    }
    embedding_calls = None
    embedding_failed = None
    schema = remote_schema()
    remote_started_at = time.monotonic()

    with connect() as conn:
        users_ok = _admin_console_table_has_columns(conn, schema, "users", {"id", "username", "created_at"})
        item_status_ok = _admin_console_table_has_columns(
            conn, schema, "item_status", {"user_id", "item_id", "read_at", "clicked_at", "starred_at"}
        )
        cluster_status_ok = _admin_console_table_has_columns(
            conn, schema, "cluster_status", {"user_id", "cluster_id", "clicked_at", "starred_at"}
        )
        fetch_runs_ok = _admin_console_table_has_columns(
            conn, schema, "fetch_runs", {"id", "started_at", "finished_at", "status", "error_msg"}
        )
        items_ok = _admin_console_table_has_columns(conn, schema, "items", {"platform", "fetched_at"})
        embedding_ok = _admin_console_table_has_columns(
            conn, schema, "embedding_usage_logs", {"created_at", "status", "estimated_cost_yuan"}
        )

        if users_ok:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total_users,
                           COUNT(*) FILTER (
                             WHERE timezone('Asia/Shanghai', created_at)::date = %(today)s::date
                           ) AS new_users_today,
                           COUNT(*) FILTER (
                             WHERE created_at >= %(since_7d)s
                           ) AS new_users_7d
                      FROM {schema}.users""",
                {"today": today, "since_7d": since_7d},
            ).fetchone()
            c_metrics["total_users"] = _admin_console_int((row or {}).get("total_users"))
            c_metrics["new_users_today"] = _admin_console_int((row or {}).get("new_users_today"))
            c_metrics["new_users_7d"] = _admin_console_int((row or {}).get("new_users_7d"))

            latest = conn.execute(
                f"""SELECT username, created_at
                      FROM {schema}.users
                     ORDER BY created_at DESC
                     LIMIT 1"""
            ).fetchone()
            if latest:
                interactions_detail["latest_signup"] = {
                    "username": latest.get("username"),
                    "created_at": _admin_console_to_shanghai_iso(latest.get("created_at")),
                }

        if item_status_ok and cluster_status_ok:
            for key, since in (("active_users_1d", since_1d), ("active_users_7d", since_7d)):
                row = conn.execute(
                    f"""SELECT COUNT(DISTINCT user_id) AS active_users
                          FROM (
                            SELECT user_id
                              FROM {schema}.item_status
                             WHERE read_at >= %(since)s
                                OR clicked_at >= %(since)s
                                OR starred_at >= %(since)s
                            UNION
                            SELECT user_id
                              FROM {schema}.cluster_status
                             WHERE clicked_at >= %(since)s
                                OR starred_at >= %(since)s
                          ) active_users""",
                    {"since": since},
                ).fetchone()
                c_metrics[key] = _admin_console_int((row or {}).get("active_users"))

        if item_status_ok:
            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS info_click_users_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS info_click_items_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE clicked_at IS NOT NULL
                           ) AS info_click_items_total
                      FROM {schema}.item_status""",
                {"since_7d": since_7d},
            ).fetchone()
            c_metrics["info_click_users_7d"] = _admin_console_int((row or {}).get("info_click_users_7d"))
            c_metrics["info_click_items_7d"] = _admin_console_int((row or {}).get("info_click_items_7d"))
            c_metrics["info_click_items_total"] = _admin_console_int((row or {}).get("info_click_items_total"))

            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE starred_at IS NOT NULL
                           ) AS starred_users,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE starred_at IS NOT NULL
                           ) AS starred_total,
                           COUNT(DISTINCT user_id) FILTER (
                             WHERE read_at >= %(since_7d)s
                           ) AS read_users_7d,
                           COUNT(DISTINCT item_id) FILTER (
                             WHERE read_at >= %(since_7d)s
                           ) AS read_items_7d
                      FROM {schema}.item_status""",
                {"since_7d": since_7d},
            ).fetchone()
            interactions_detail["starred_users"] = _admin_console_int((row or {}).get("starred_users"))
            interactions_detail["starred_total"] = _admin_console_int((row or {}).get("starred_total"))
            interactions_detail["read_users_7d"] = _admin_console_int((row or {}).get("read_users_7d"))
            interactions_detail["read_items_7d"] = _admin_console_int((row or {}).get("read_items_7d"))

        if cluster_status_ok:
            row = conn.execute(
                f"""SELECT COUNT(DISTINCT user_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS highlight_click_users_7d,
                           COUNT(DISTINCT cluster_id) FILTER (
                             WHERE clicked_at >= %(since_7d)s
                           ) AS highlight_click_events_7d,
                           COUNT(DISTINCT cluster_id) FILTER (
                             WHERE clicked_at IS NOT NULL
                           ) AS highlight_click_events_total
                      FROM {schema}.cluster_status""",
                {"since_7d": since_7d},
            ).fetchone()
            c_metrics["highlight_click_users_7d"] = _admin_console_int((row or {}).get("highlight_click_users_7d"))
            c_metrics["highlight_click_events_7d"] = _admin_console_int((row or {}).get("highlight_click_events_7d"))
            c_metrics["highlight_click_events_total"] = _admin_console_int((row or {}).get("highlight_click_events_total"))

        if embedding_ok:
            row = conn.execute(
                f"""SELECT COUNT(*) AS embedding_calls_24h,
                           COUNT(*) FILTER (
                             WHERE status != 'success'
                           ) AS embedding_failed_24h,
                           COALESCE(SUM(estimated_cost_yuan), 0.0) AS embedding_cost_yuan_24h
                      FROM {schema}.embedding_usage_logs
                     WHERE created_at >= %(since_24h)s""",
                {"since_24h": since_24h},
            ).fetchone()
            embedding_calls = _admin_console_int((row or {}).get("embedding_calls_24h"))
            embedding_failed = _admin_console_int((row or {}).get("embedding_failed_24h"))
            cost["embedding_calls_24h"] = embedding_calls
            cost["embedding_cost_yuan_24h"] = _admin_console_float((row or {}).get("embedding_cost_yuan_24h"))

        latest_run = None
        pipeline_counts = None
        if fetch_runs_ok:
            latest_run = conn.execute(
                f"""SELECT id, started_at, finished_at, status, error_msg
                      FROM {schema}.fetch_runs
                     ORDER BY id DESC
                     LIMIT 1"""
            ).fetchone()
            pipeline_counts = conn.execute(
                f"""SELECT COUNT(*) FILTER (
                             WHERE COALESCE(finished_at, started_at) >= %(since_24h)s
                           ) AS total_runs_24h,
                           COUNT(*) FILTER (
                             WHERE status = 'success'
                               AND COALESCE(finished_at, started_at) >= %(since_24h)s
                           ) AS success_runs_24h,
                           COUNT(*) FILTER (
                             WHERE status = 'success'
                               AND COALESCE(finished_at, started_at) >= %(since_48h)s
                           ) AS success_runs_48h
                      FROM {schema}.fetch_runs""",
                {"since_24h": since_24h, "since_48h": since_48h},
            ).fetchone()

        freshness_rows = []
        if items_ok:
            freshness_rows = conn.execute(
                f"""SELECT platform, MAX(fetched_at) AS last_fetched_at
                      FROM {schema}.items
                     WHERE platform IS NOT NULL
                     GROUP BY platform"""
            ).fetchall()

        db_size = _admin_console_db_size(conn)

        if users_ok:
            user_trend_rows = conn.execute(
                f"""SELECT to_char(days.day, 'YYYY-MM-DD') AS date,
                           COALESCE(counts.value, 0)::int AS value
                      FROM generate_series(%(start_date)s::date, %(end_date)s::date, interval '1 day') AS days(day)
                 LEFT JOIN (
                           SELECT timezone('Asia/Shanghai', created_at)::date AS day,
                                  COUNT(*)::int AS value
                             FROM {schema}.users
                            WHERE timezone('Asia/Shanghai', created_at)::date >= %(start_date)s::date
                         GROUP BY day
                      ) counts ON counts.day = days.day
                  ORDER BY days.day""",
                {"start_date": start_14d, "end_date": today},
            ).fetchall()
            new_users_14d = _admin_console_trend(user_trend_rows, now_shanghai, 14, 0)
        else:
            new_users_14d = _admin_console_date_points(now_shanghai, 14, None)

        if fetch_runs_ok:
            fetch_trend_rows = conn.execute(
                f"""SELECT to_char(days.day, 'YYYY-MM-DD') AS date,
                           CASE
                             WHEN counts.total_runs IS NULL OR counts.total_runs = 0 THEN NULL
                             ELSE counts.success_runs::float / counts.total_runs
                           END AS value
                      FROM generate_series(%(start_date)s::date, %(end_date)s::date, interval '1 day') AS days(day)
                 LEFT JOIN (
                           SELECT timezone('Asia/Shanghai', COALESCE(finished_at, started_at))::date AS day,
                                  COUNT(*)::int AS total_runs,
                                  COUNT(*) FILTER (WHERE status = 'success')::int AS success_runs
                             FROM {schema}.fetch_runs
                            WHERE COALESCE(finished_at, started_at) IS NOT NULL
                              AND timezone('Asia/Shanghai', COALESCE(finished_at, started_at))::date >= %(start_date)s::date
                         GROUP BY day
                      ) counts ON counts.day = days.day
                  ORDER BY days.day""",
                {"start_date": start_7d, "end_date": today},
            ).fetchall()
            fetch_success_rate_7d = _admin_console_trend(fetch_trend_rows, now_shanghai, 7, None)
        else:
            fetch_success_rate_7d = _admin_console_date_points(now_shanghai, 7, None)

    signals = [
        _admin_console_pipeline_signal(dict(latest_run) if latest_run else None, dict(pipeline_counts) if pipeline_counts else None, now_utc),
        _admin_console_freshness_signal(freshness_rows, now_utc),
        _admin_console_embedding_signal(embedding_calls, embedding_failed),
        _admin_console_remote_db_signal(remote_started_at),
        _admin_console_disk_signal(_admin_console_disk_usage_percent(), db_size),
    ]

    return {
        "available": True,
        "generated_at": now_shanghai.isoformat(),
        "c_metrics": c_metrics,
        "interactions_detail": interactions_detail,
        "cost": cost,
        "health": {
            "signals": signals,
            "incidents": _admin_console_incidents(signals),
        },
        "trends": {
            "new_users_14d": new_users_14d,
            "fetch_success_rate_7d": fetch_success_rate_7d,
        },
    }


def _empty_embedding_usage(hours: float = 24, limit: int = 50) -> dict[str, Any]:
    return {
        "hours": hours,
        "run_id": None,
        "summary": {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "input_count": 0,
            "input_chars": 0,
            "input_bytes": 0,
            "estimated_tokens_attempted": 0,
            "estimated_tokens_success": 0,
            "output_count": 0,
            "estimated_cost_yuan_success": 0.0,
            "estimated_cost_yuan_all": 0.0,
        },
        "by_source": [],
        "by_run": [],
        "logs": [],
        "limit": max(1, min(int(limit or 50), 500)),
    }


def vector_to_pg(value: Any) -> str | None:
    if value is None:
        return None
    return "[" + ",".join(f"{float(x):.9g}" for x in value) + "]"


def pg_vector_to_list(value: Any) -> list[float] | None:
    """Parse a pgvector value returned by psycopg into a Python float list."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    return [float(part.strip()) for part in text.split(",") if part.strip()]


def update_item_embedding_remote(
    pg_conn: Any | None,
    item_id: str,
    vector: Any,
    provider_name: str,
    *,
    model: str | None = None,
    input_variant: str | None = None,
) -> None:
    """Write an item embedding to Supabase pgvector."""
    if pg_conn is None:
        with connect() as conn:
            update_item_embedding_remote(
                conn,
                item_id,
                vector,
                provider_name,
                model=model,
                input_variant=input_variant,
            )
            return

    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET embedding = %s::extensions.vector,
                   embedding_provider = %s,
                   embedding_model = COALESCE(%s, embedding_model),
                   embedding_input_variant = COALESCE(%s, embedding_input_variant),
                   embedding_generated_at = now()
             WHERE id = %s""",
        (
            vector_to_pg(vector),
            provider_name,
            model,
            input_variant,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def add_item_to_cluster_remote(
    pg_conn: Any | None,
    cluster_id: int,
    item_id: str,
    *,
    rank_in_cluster: int = 9999,
    is_primary_source: int | bool = 0,
    source_identity: str | None = None,
    join_decision_id: int | None = None,
) -> None:
    """Write cluster membership and item.cluster_id directly to Supabase."""
    if pg_conn is None:
        with connect() as conn:
            add_item_to_cluster_remote(
                conn,
                cluster_id,
                item_id,
                rank_in_cluster=rank_in_cluster,
                is_primary_source=is_primary_source,
                source_identity=source_identity,
                join_decision_id=join_decision_id,
            )
            return

    schema = remote_schema()
    pg_conn.execute(
        f"""INSERT INTO {schema}.cluster_items
              (cluster_id, item_id, rank_in_cluster, added_at, is_primary_source,
               source_identity, join_decision_id)
            VALUES (%s, %s, %s, now(), %s, %s, %s)
            ON CONFLICT (cluster_id, item_id) DO NOTHING""",
        (
            cluster_id,
            item_id,
            rank_in_cluster,
            bool(is_primary_source),
            source_identity,
            str(join_decision_id) if join_decision_id is not None else None,
        ),
    )
    pg_conn.execute(
        f"UPDATE {schema}.items SET cluster_id = %s WHERE id = %s",
        (cluster_id, item_id),
    )
    _commit_if_supported(pg_conn)


def mark_cluster_touched_by_run_remote(
    pg_conn: Any | None,
    cluster_id: int,
    run_id: int | None,
) -> None:
    if run_id is None:
        return
    if pg_conn is None:
        with connect() as conn:
            mark_cluster_touched_by_run_remote(conn, cluster_id, run_id)
            return
    pg_conn.execute(
        f"UPDATE {remote_schema()}.clusters SET last_touched_run_id = %s WHERE id = %s",
        (run_id, cluster_id),
    )
    _commit_if_supported(pg_conn)


def finalize_cluster_state_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    tau_hours: float,
) -> dict[str, Any]:
    """Recompute derived cluster fields in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return finalize_cluster_state_remote(conn, cluster_id, tau_hours=tau_hours)

    schema = remote_schema()
    set_cluster_write_statement_timeout(pg_conn)
    counts = pg_conn.execute(
        f"""SELECT
              COUNT(DISTINCT (i.platform, COALESCE(i.author_name, i.id))) AS doc_count,
              COUNT(DISTINCT ci.source_identity)
                FILTER (WHERE ci.source_identity IS NOT NULL) AS unique_source_count
            FROM {schema}.cluster_items ci
            JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchone()
    doc_count = int(_row_get(counts, "doc_count", 0) or 0)
    unique_source_count = int(_row_get(counts, "unique_source_count", 0) or 0)

    platform_rows = pg_conn.execute(
        f"""SELECT DISTINCT i.platform
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchall()
    platforms = sorted({
        _row_get(row, "platform")
        for row in platform_rows
        if _row_get(row, "platform")
    })

    bounds = pg_conn.execute(
        f"""SELECT MIN(COALESCE(i.published_at, i.fetched_at)) AS first_doc_at,
                  MAX(COALESCE(i.published_at, i.fetched_at)) AS last_doc_at
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchone()
    now = datetime.now(timezone.utc)
    first_doc_at = _timestamp_value(_row_get(bounds, "first_doc_at")) or to_utc_iso(now)
    last_doc_at = _timestamp_value(_row_get(bounds, "last_doc_at")) or first_doc_at

    vector_rows = pg_conn.execute(
        f"""SELECT i.embedding::text AS embedding_text,
                  COALESCE(i.published_at, i.fetched_at) AS ts
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = %s
              AND i.embedding IS NOT NULL""",
        (cluster_id,),
    ).fetchall()
    representative_vector = None
    vecs = []
    timestamps = []
    if vector_rows:
        try:
            import numpy as np
            from clustering import vector_utils as vu
            from time_utils import parse_datetime

            for row in vector_rows:
                parsed_vector = pg_vector_to_list(_row_get(row, "embedding_text"))
                if parsed_vector is None:
                    continue
                vecs.append(np.asarray(parsed_vector, dtype=np.float32))
                timestamps.append(parse_datetime(_row_get(row, "ts")) or now)
            representative_vector = vu.weighted_mean_with_decay(
                vecs,
                timestamps,
                now=now,
                tau_hours=tau_hours,
            ) if vecs else None
        except Exception:
            representative_vector = None

    pg_conn.execute(
        f"""UPDATE {schema}.clusters
               SET doc_count = %s,
                   unique_source_count = %s,
                   platforms_json = %s,
                   first_doc_at = %s,
                   last_doc_at = %s,
                   last_updated_at = %s,
                   representative_vector = COALESCE(%s::extensions.vector, representative_vector)
             WHERE id = %s""",
        (
            doc_count,
            unique_source_count,
            _maybe_jsonb(platforms),
            first_doc_at,
            last_doc_at,
            now,
            vector_to_pg(representative_vector),
            cluster_id,
        ),
    )
    _commit_if_supported(pg_conn)
    return {
        "cluster_id": cluster_id,
        "doc_count": doc_count,
        "unique_source_count": unique_source_count,
        "platforms": platforms,
        "first_doc_at": first_doc_at,
        "last_doc_at": last_doc_at,
    }


def create_singleton_cluster_remote(
    pg_conn: Any | None,
    item_id: str,
    vector: Any,
    first_doc_at: Any,
    *,
    source_identity: str | None = None,
    run_id: int | None = None,
    tau_hours: float = 24.0,
) -> int:
    """Create a singleton event cluster in Supabase and attach the seed item."""
    if pg_conn is None:
        with connect() as conn:
            return create_singleton_cluster_remote(
                conn,
                item_id,
                vector,
                first_doc_at,
                source_identity=source_identity,
                run_id=run_id,
                tau_hours=tau_hours,
            )

    schema = remote_schema()
    event_time = _timestamp_value(first_doc_at) or datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    _ensure_remote_id_sequence(pg_conn, "clusters")
    set_cluster_write_statement_timeout(pg_conn)
    row = pg_conn.execute(
        f"""INSERT INTO {schema}.clusters
              (first_doc_at, last_doc_at, last_updated_at,
               doc_count, unique_source_count,
               platforms_json, is_visible_in_feed,
               created_run_id, last_touched_run_id, created_at)
            VALUES (%s, %s, %s, 1, 1, '[]'::jsonb,
                    false, %s, %s, %s)
            RETURNING id""",
        (
            event_time,
            event_time,
            now,
            run_id,
            run_id,
            now,
        ),
    ).fetchone()
    cluster_id = _row_id(row)
    if source_identity is None:
        seed = pg_conn.execute(
            f"SELECT id, url FROM {schema}.items WHERE id = %s",
            (item_id,),
        ).fetchone()
        source_identity = _source_identity_from_row(seed) if seed is not None else item_id
    add_item_to_cluster_remote(
        pg_conn,
        cluster_id,
        item_id,
        rank_in_cluster=0,
        is_primary_source=True,
        source_identity=source_identity,
    )
    finalize_cluster_state_remote(pg_conn, cluster_id, tau_hours=tau_hours)
    mark_cluster_touched_by_run_remote(pg_conn, cluster_id, run_id)
    _commit_if_supported(pg_conn)
    return cluster_id


def recall_top_k_clusters_remote(
    pg_conn: Any | None,
    vector: Any,
    *,
    k: int = 10,
    window_days: int = 30,
    cosine_min: float = 0.0,
    item_time: str | datetime | None = None,
    temporal_adjacency_days: float | None = None,
    max_merged_span_days: float | None = None,
) -> list[dict[str, Any]]:
    """Return top-K cluster recall candidates from Supabase pgvector."""
    if pg_conn is None:
        with connect() as conn:
            return recall_top_k_clusters_remote(
                conn,
                vector,
                k=k,
                window_days=window_days,
                cosine_min=cosine_min,
                item_time=item_time,
                temporal_adjacency_days=temporal_adjacency_days,
                max_merged_span_days=max_merged_span_days,
            )

    schema = remote_schema()
    query_vector = vector_to_pg(vector)
    item_dt = parse_datetime(item_time)
    where = [
        "c.representative_vector IS NOT NULL",
        "COALESCE(c.archived, false) = false",
        "c.merged_into IS NULL",
    ]
    params: list[Any] = [query_vector]
    if item_dt is not None:
        cluster_first = "COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)"
        cluster_last = "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at)"
        adjacency_days = 3.0 if temporal_adjacency_days is None else max(0.0, float(temporal_adjacency_days))
        where.append(
            f"""{cluster_first} <= %s::timestamptz + (%s::double precision * interval '1 day')
                AND {cluster_last} >= %s::timestamptz - (%s::double precision * interval '1 day')"""
        )
        params.extend([item_dt, adjacency_days, item_dt, adjacency_days])
        if max_merged_span_days is not None:
            max_span_days = max(0.0, float(max_merged_span_days))
            where.append(
                f"""EXTRACT(EPOCH FROM (
                       GREATEST({cluster_last}, %s::timestamptz)
                       - LEAST({cluster_first}, %s::timestamptz)
                    )) / 86400.0 <= %s"""
            )
            params.extend([item_dt, item_dt, max_span_days])
    elif window_days:
        where.append("c.last_updated_at > now() - (%s::int * interval '1 day')")
        params.append(int(window_days))
    if cosine_min:
        where.append("1 - (c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector) >= %s")
        params.extend([query_vector, float(cosine_min)])
    params.extend([query_vector, max(0, int(k))])
    rows = pg_conn.execute(
        f"""SELECT c.id AS cluster_id,
                  c.representative_vector::text AS representative_vector,
                  1 - (c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector) AS cosine,
                  c.doc_count,
                  c.live_version,
                  c.first_doc_at,
                  c.last_doc_at,
                  c.last_updated_at,
                  c.ai_title,
                  c.ai_summary,
                  c.ai_key_points
             FROM {schema}.clusters c
            WHERE {' AND '.join(where)}
            ORDER BY c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector
            LIMIT %s""",
        tuple(params),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["representative_vector"] = pg_vector_to_list(item.get("representative_vector"))
        for key in ("first_doc_at", "last_doc_at", "last_updated_at"):
            if key in item:
                item[key] = _timestamp_value(item.get(key))
        out.append(item)
    return out


def recall_clusters_by_member_item_ids_remote(
    pg_conn: Any | None,
    item_ids: list[str],
    vector: Any,
) -> list[dict[str, Any]]:
    """Return active clusters that contain explicitly referenced source items."""
    if pg_conn is None:
        with connect() as conn:
            return recall_clusters_by_member_item_ids_remote(conn, item_ids, vector)
    if not item_ids:
        return []

    schema = remote_schema()
    query_vector = vector_to_pg(vector)
    rows = pg_conn.execute(
        f"""SELECT DISTINCT ON (c.id)
                  c.id AS cluster_id,
                  c.representative_vector::text AS representative_vector,
                  1 - (c.representative_vector OPERATOR(extensions.<=>) %s::extensions.vector) AS cosine,
                  c.doc_count,
                  c.live_version,
                  c.first_doc_at,
                  c.last_doc_at,
                  c.last_updated_at,
                  c.ai_title,
                  c.ai_summary,
                  c.ai_key_points,
                  ci.item_id AS referenced_item_id
             FROM {schema}.cluster_items ci
             JOIN {schema}.clusters c ON c.id = ci.cluster_id
            WHERE ci.item_id = ANY(%s)
              AND c.representative_vector IS NOT NULL
              AND COALESCE(c.archived, false) = false
              AND c.merged_into IS NULL
            ORDER BY c.id, ci.item_id""",
        (query_vector, item_ids),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["representative_vector"] = pg_vector_to_list(item.get("representative_vector"))
        for key in ("first_doc_at", "last_doc_at", "last_updated_at"):
            item[key] = _timestamp_value(item.get(key))
        item["recall_band"] = "explicit_reference"
        out.append(item)
    return out


def mark_cluster_hidden_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    warning: str,
    publish_immediately: bool,
    run_id: int | None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            mark_cluster_hidden_remote(
                conn,
                cluster_id,
                warning=warning,
                publish_immediately=publish_immediately,
                run_id=run_id,
            )
            return
    schema = remote_schema()
    warnings_json = [warning]
    if not publish_immediately:
        pg_conn.execute(
            f"""UPDATE {schema}.clusters
                   SET pending_is_visible_in_feed = 0,
                       pending_summary_warnings_json = %s,
                       last_touched_run_id = COALESCE(%s, last_touched_run_id)
                 WHERE id = %s""",
            (_maybe_jsonb(warnings_json), run_id, cluster_id),
        )
    else:
        now = datetime.now(timezone.utc)
        pg_conn.execute(
            f"""UPDATE {schema}.clusters
                   SET is_visible_in_feed = false,
                       last_summary_warnings_json = %s,
                       last_updated_at = %s,
                       published_at = %s,
                       published_run_id = COALESCE(%s, published_run_id)
                 WHERE id = %s""",
            (_maybe_jsonb(warnings_json), now, now, run_id, cluster_id),
        )
    _commit_if_supported(pg_conn)


def get_cluster_summary_context_remote(pg_conn: Any | None, cluster_id: int) -> dict[str, Any] | None:
    if pg_conn is None:
        with connect() as conn:
            return get_cluster_summary_context_remote(conn, cluster_id)
    row = pg_conn.execute(
        f"""SELECT id, live_version, doc_count, unique_source_count
              FROM {remote_schema()}.clusters
             WHERE id = %s""",
        (cluster_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def collect_cluster_member_rows_remote(
    pg_conn: Any | None,
    cluster_id: int,
) -> list[dict[str, Any]]:
    if pg_conn is None:
        with connect() as conn:
            return collect_cluster_member_rows_remote(conn, cluster_id)
    rows = pg_conn.execute(
        f"""SELECT i.id, i.title, i.content, i.asr_text, i.author_name, i.platform, i.url,
                  i.detail_json,
                  i.ai_summary, i.ai_key_points, i.ai_category,
                  i.published_at, i.fetched_at,
                  ci.is_primary_source, ci.rank_in_cluster
             FROM {remote_schema()}.items i
             JOIN {remote_schema()}.cluster_items ci ON ci.item_id = i.id
            WHERE ci.cluster_id = %s""",
        (cluster_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def cluster_dominant_category_remote(pg_conn: Any | None, cluster_id: int) -> str | None:
    rows = collect_cluster_member_rows_remote(pg_conn, cluster_id)
    from clustering import visibility_policy

    return visibility_policy.dominant_category(row.get("ai_category") for row in rows)


def write_cluster_summary_draft_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    title: str,
    summary: str,
    key_points: Any,
    why_read: str | None,
    is_visible: bool,
    warnings: list[str],
    run_id: int | None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            write_cluster_summary_draft_remote(
                conn,
                cluster_id,
                title=title,
                summary=summary,
                key_points=key_points,
                why_read=why_read,
                is_visible=is_visible,
                warnings=warnings,
                run_id=run_id,
            )
            return
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.clusters
               SET ai_title_draft = %s,
                   ai_summary_draft = %s,
                   ai_key_points_draft = %s,
                   why_read = %s,
                   pending_is_visible_in_feed = %s,
                   pending_summary_warnings_json = %s,
                   last_touched_run_id = COALESCE(%s, last_touched_run_id)
             WHERE id = %s""",
        (
            title,
            summary,
            json.dumps(key_points, ensure_ascii=False),
            why_read,
            1 if is_visible else 0,
            _maybe_jsonb(warnings),
            run_id,
            cluster_id,
        ),
    )
    _commit_if_supported(pg_conn)


def publish_cluster_summary_live_remote(
    pg_conn: Any | None,
    cluster_id: int,
    *,
    is_visible: bool,
    warnings: list[str],
    run_id: int | None,
    new_version: int,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            publish_cluster_summary_live_remote(
                conn,
                cluster_id,
                is_visible=is_visible,
                warnings=warnings,
                run_id=run_id,
                new_version=new_version,
            )
            return
    now = datetime.now(timezone.utc)
    schema = remote_schema()
    pg_conn.execute(
        f"""UPDATE {schema}.clusters
               SET ai_title = ai_title_draft,
                   ai_summary = ai_summary_draft,
                   ai_key_points = ai_key_points_draft,
                   ai_title_draft = NULL,
                   ai_summary_draft = NULL,
                   ai_key_points_draft = NULL,
                   is_visible_in_feed = %s,
                   last_summary_warnings_json = %s,
                   pending_is_visible_in_feed = NULL,
                   pending_summary_warnings_json = NULL,
                   last_updated_at = %s,
                   published_at = %s,
                   published_run_id = COALESCE(%s, published_run_id)
             WHERE id = %s""",
        (
            bool(is_visible),
            _maybe_jsonb(warnings),
            now,
            now,
            run_id,
            cluster_id,
        ),
    )
    bump_cluster_version_and_stale_actions_remote(pg_conn, cluster_id, new_version)
    _commit_if_supported(pg_conn)


def fetch_high_score_items_remote(min_score: int = 6, hours: int = 24) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, platform, source, title, ai_summary, ai_category, ai_keywords,
                      relevance_score, author_name, url, fetched_at
                 FROM {remote_schema()}.items
                WHERE fetched_at > now() - (%s::int * interval '1 hour')
                  AND relevance_score >= %s
                ORDER BY relevance_score DESC""",
            (int(hours), min_score),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["ai_keywords"] = _json_value(item.get("ai_keywords"))
        item["fetched_at"] = _timestamp_value(item.get("fetched_at"))
        out.append(item)
    return out


def get_feedback_scores_remote() -> dict[str, Any]:
    schema = remote_schema()
    with connect() as conn:
        author_rows = conn.execute(
            f"""SELECT i.author_name, f.type, COUNT(*) AS cnt
                  FROM {schema}.feedback f
                  JOIN {schema}.items i ON f.item_id = i.id
                 WHERE f.type IN ('positive', 'low_quality')
                   AND COALESCE(i.author_name, '') != ''
                 GROUP BY i.author_name, f.type"""
        ).fetchall()
        item_rows = conn.execute(
            f"SELECT item_id, type FROM {schema}.feedback"
        ).fetchall()
        text_rows = conn.execute(
            f"""SELECT item_id, text, created_at
                  FROM {schema}.feedback
                 WHERE type = 'text'
                   AND text IS NOT NULL
                   AND text != ''
                 ORDER BY created_at DESC"""
        ).fetchall()

    author_scores: dict[str, int] = {}
    for row in author_rows:
        name = row["author_name"]
        author_scores.setdefault(name, 0)
        author_scores[name] += int(row["cnt"] or 0) if row["type"] == "positive" else -int(row["cnt"] or 0)
    item_feedback: dict[str, list[str]] = {}
    for row in item_rows:
        item_feedback.setdefault(row["item_id"], []).append(row["type"])
    text_feedback: dict[str, list[dict[str, Any]]] = {}
    for row in text_rows:
        text_feedback.setdefault(row["item_id"], []).append({
            "text": row["text"],
            "created_at": _timestamp_value(row.get("created_at")),
        })
    return {
        "author_scores": author_scores,
        "item_feedback": item_feedback,
        "text_feedback": text_feedback,
    }


def _display_score_from_max_flag_score10(value: Any) -> int | None:
    if value is None:
        return None
    try:
        score10 = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score10):
        return None
    return int(round(score10 * 10))


def _public_cluster_filter(schema: str, cluster_alias: str = "c") -> str:
    return f"""
      AND NOT EXISTS (
        SELECT 1
        FROM {schema}.cluster_items ci_priv
        JOIN {schema}.items i_priv ON i_priv.id = ci_priv.item_id
        WHERE ci_priv.cluster_id = {cluster_alias}.id
          AND (i_priv.platform = 'manual' OR i_priv.user_id IS NOT NULL)
      )
    """


def _github_display_filter(schema: str, min_stars: int, cluster_alias: str = "c") -> str:
    if min_stars <= 0:
        return ""
    return f"""
      AND (
        NOT EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          WHERE ci_disp.cluster_id = {cluster_alias}.id
        )
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          JOIN {schema}.items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform != 'github'
        )
        OR EXISTS (
          SELECT 1
          FROM {schema}.cluster_items ci_disp
          JOIN {schema}.items i_disp ON i_disp.id = ci_disp.item_id
          WHERE ci_disp.cluster_id = {cluster_alias}.id
            AND i_disp.platform = 'github'
            AND CASE
              WHEN i_disp.metrics_json ? 'stars'
                   AND (i_disp.metrics_json ->> 'stars') ~ '^[0-9]+$'
                THEN (i_disp.metrics_json ->> 'stars')::integer
              ELSE 0
            END >= {int(min_stars)}
        )
      )
    """


def _overlay_user_cluster_seen(result: dict, user_id: str) -> None:
    """In-place 覆盖逐用户 seen 字段;调用方持有的是缓存的深拷贝,可安全改。"""
    events = result.get("events") or []
    ids = [int(ev["id"]) for ev in events if ev.get("id") is not None]
    if not ids:
        return
    schema = remote_schema()
    try:
        with connect() as conn:
            seen_rows = conn.execute(
                f"""SELECT cluster_id, last_seen_version
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = ANY(%(cluster_ids)s)""",
                {"user_id": user_id, "cluster_ids": ids},
            ).fetchall()
    except Exception:
        return  # 状态覆盖失败:内容照常返回,seen 字段保持匿名默认值
    seen_map = {int(r["cluster_id"]): int(r["last_seen_version"] or 0) for r in seen_rows}
    for ev in events:
        cid = ev.get("id")
        if cid is None:
            continue
        seen = seen_map.get(int(cid))
        live_version = int(ev.get("live_version") or 0)
        ev["last_seen_version"] = seen
        ev["has_update"] = bool(seen is not None and live_version > seen)


def cluster_detail(*, cluster_id: int, public_only: bool = False,
                   user_id: str | None = None) -> dict | None:
    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.ai_key_points, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version, c.merged_into,
                       c.is_visible_in_feed
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
                  ) detail_cover ON true
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        metadata = (
            _fetch_event_source_metadata(conn, schema, [cluster_id]).get(cluster_id, {})
            if row else {}
        )
    if not row:
        return None
    data = dict(row)
    user_last_seen = None
    viewer_status = {
        "clicked_at": None,
        "starred_at": None,
        "last_seen_version": None,
        "feedback_kind": None,
        "feedback_note": None,
    }
    if user_id:
        with connect() as conn:
            seen = conn.execute(
                f"""SELECT clicked_at, starred_at, last_seen_version, feedback_kind, feedback_note
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
        if seen:
            user_last_seen = int(seen["last_seen_version"]) if seen["last_seen_version"] is not None else None
            viewer_status = {
                "clicked_at": to_utc_iso(seen.get("clicked_at")) if seen.get("clicked_at") else None,
                "starred_at": to_utc_iso(seen.get("starred_at")) if seen.get("starred_at") else None,
                "last_seen_version": user_last_seen,
                "feedback_kind": seen.get("feedback_kind"),
                "feedback_note": seen.get("feedback_note"),
            }
    body = {
        "id": int(data["id"]),
        "ai_title": data.get("ai_title"),
        "ai_summary": data.get("ai_summary"),
        "why_read": data.get("why_read"),
        "ai_key_points": _json_array(data.get("ai_key_points")),
        "doc_count": int(data.get("doc_count") or 0),
        "unique_source_count": int(data.get("unique_source_count") or 0),
        "platforms": _json_array(data.get("platforms_json")),
        "category": metadata.get("category"),
        "first_doc_at": to_utc_iso(data.get("first_doc_at")) or data.get("first_doc_at"),
        "last_doc_at": to_utc_iso(data.get("last_doc_at")) if data.get("last_doc_at") else None,
        "cover_url": data.get("cover_url"),
        "media_urls": _media_urls_from_item(data.get("cover_url"), None),
        "media": metadata.get("media", []),
        "live_version": int(data.get("live_version") or 0),
        "user_last_seen_version": user_last_seen,
        "viewer_status": viewer_status,
        "is_visible_in_feed": bool(data.get("is_visible_in_feed")),
        "data_backend": event_read_backend(),
    }
    if data.get("merged_into"):
        body["redirect_to"] = int(data["merged_into"])
    return body


def cluster_sources(
    *,
    cluster_id: int,
    page: int = 1,
    limit: int = 20,
    public_only: bool = False,
    user_id: str | None = None,
) -> dict | None:
    schema = remote_schema()
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    source_where, source_params = _manual_item_filter(
        "i", public_only=public_only, manual_owner_user_id=user_id
    )
    source_filter = (" AND " + " AND ".join(source_where)) if source_where else ""
    with connect() as conn:
        exists = conn.execute(
            f"""SELECT 1
                  FROM {schema}.clusters c
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        if not exists:
            return None
        rows = conn.execute(
            f"""SELECT i.id AS item_id, i.title, i.author_name, i.platform,
                       i.published_at, i.fetched_at, i.url, ci.is_primary_source,
                       i.cover_url, i.media_json,
                       left(coalesce(i.ai_summary, i.content, ''), 200) AS snippet
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %(cluster_id)s
                 {source_filter}
                 ORDER BY coalesce(i.published_at, i.fetched_at) DESC,
                          ci.is_primary_source DESC
                 LIMIT %(limit_plus_one)s OFFSET %(offset)s""",
            {
                "cluster_id": cluster_id,
                "limit_plus_one": limit + 1,
                "offset": offset,
                **source_params,
            },
        ).fetchall()
    has_more = len(rows) > limit
    sources = []
    for raw in rows[:limit]:
        r = dict(raw)
        platform = r.get("platform") or ""
        badge = None
        if platform in ("openai", "anthropic", "official"):
            badge = "official"
        elif platform in ("hackernews",):
            badge = "community"
        source_media = normalize_media(r.get("cover_url"), r.get("media_json"), source_url=r.get("url"))
        sources.append({
            "item_id": r.get("item_id"),
            "title": r.get("title"),
            "author": r.get("author_name"),
            "platform": platform,
            "published_at": to_utc_iso(r.get("published_at") or r.get("fetched_at")),
            "url": r.get("url"),
            "cover_url": r.get("cover_url"),
            "media_urls": image_urls(source_media),
            "media": source_media,
            "is_primary_source": int(bool(r.get("is_primary_source"))),
            "authority_badge": badge,
            "snippet": (r.get("snippet") or "").strip(),
        })
    return {
        "sources": sources,
        "next_cursor": (page + 1) if has_more else None,
        "data_backend": event_read_backend(),
    }


def cluster_bundle(
    *,
    cluster_id: int,
    page: int = 1,
    limit: int = 20,
    public_only: bool = False,
    user_id: str | None = None,
) -> dict | None:
    """Return cluster detail and first-page sources in a single DB checkout."""
    schema = remote_schema()
    cache_key = (
        "cluster_bundle",
        schema,
        int(cluster_id),
        int(page),
        int(limit),
        bool(public_only),
        user_id or "",
    )
    cached = _cache_get_copy_with_ttl(cache_key, _cluster_bundle_cache_ttl_sec())
    if cached is not None:
        return cached
    offset = (page - 1) * limit
    public_filter = _public_cluster_filter(schema, "c") if public_only else ""
    source_where, source_params = _manual_item_filter(
        "i", public_only=public_only, manual_owner_user_id=user_id
    )
    source_filter = (" AND " + " AND ".join(source_where)) if source_where else ""
    with connect() as conn:
        row = conn.execute(
            f"""SELECT c.id, c.ai_title, c.ai_summary, c.why_read, c.ai_key_points, c.doc_count,
                       c.unique_source_count, c.platforms_json,
                       COALESCE(NULLIF(c.cover_url, ''), detail_cover.cover_url) AS cover_url,
                       c.first_doc_at, c.last_doc_at, c.live_version, c.merged_into,
                       c.is_visible_in_feed
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
                  ) detail_cover ON true
                 WHERE c.id = %(cluster_id)s
                 {public_filter}""",
            {"cluster_id": cluster_id},
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        metadata = _fetch_event_source_metadata(conn, schema, [cluster_id]).get(cluster_id, {})
        user_last_seen = None
        viewer_status = {
            "clicked_at": None,
            "starred_at": None,
            "last_seen_version": None,
            "feedback_kind": None,
            "feedback_note": None,
        }
        if user_id:
            seen = conn.execute(
                f"""SELECT clicked_at, starred_at, last_seen_version, feedback_kind, feedback_note
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
            if seen:
                user_last_seen = int(seen["last_seen_version"]) if seen["last_seen_version"] is not None else None
                viewer_status = {
                    "clicked_at": to_utc_iso(seen.get("clicked_at")) if seen.get("clicked_at") else None,
                    "starred_at": to_utc_iso(seen.get("starred_at")) if seen.get("starred_at") else None,
                    "last_seen_version": user_last_seen,
                    "feedback_kind": seen.get("feedback_kind"),
                    "feedback_note": seen.get("feedback_note"),
                }
        source_rows = conn.execute(
            f"""SELECT i.id AS item_id, i.title, i.author_name, i.platform,
                       i.published_at, i.fetched_at, i.url, ci.is_primary_source,
                       i.cover_url, i.media_json,
                       left(coalesce(i.ai_summary, i.content, ''), 200) AS snippet
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %(cluster_id)s
                 {source_filter}
                 ORDER BY coalesce(i.published_at, i.fetched_at) DESC,
                          ci.is_primary_source DESC
                 LIMIT %(limit_plus_one)s OFFSET %(offset)s""",
            {
                "cluster_id": cluster_id,
                "limit_plus_one": limit + 1,
                "offset": offset,
                **source_params,
            },
        ).fetchall()

    detail = {
        "id": int(data["id"]),
        "ai_title": data.get("ai_title"),
        "ai_summary": data.get("ai_summary"),
        "why_read": data.get("why_read"),
        "ai_key_points": _json_array(data.get("ai_key_points")),
        "doc_count": int(data.get("doc_count") or 0),
        "unique_source_count": int(data.get("unique_source_count") or 0),
        "platforms": _json_array(data.get("platforms_json")),
        "category": metadata.get("category"),
        "first_doc_at": to_utc_iso(data.get("first_doc_at")) or data.get("first_doc_at"),
        "last_doc_at": to_utc_iso(data.get("last_doc_at")) if data.get("last_doc_at") else None,
        "cover_url": data.get("cover_url"),
        "media_urls": _media_urls_from_item(data.get("cover_url"), None),
        "media": metadata.get("media", []),
        "live_version": int(data.get("live_version") or 0),
        "user_last_seen_version": user_last_seen,
        "viewer_status": viewer_status,
        "is_visible_in_feed": bool(data.get("is_visible_in_feed")),
        "data_backend": event_read_backend(),
    }
    if data.get("merged_into"):
        detail["redirect_to"] = int(data["merged_into"])

    has_more = len(source_rows) > limit
    sources = []
    for raw in source_rows[:limit]:
        r = dict(raw)
        platform = r.get("platform") or ""
        badge = None
        if platform in ("openai", "anthropic", "official"):
            badge = "official"
        elif platform in ("hackernews",):
            badge = "community"
        source_media = normalize_media(r.get("cover_url"), r.get("media_json"), source_url=r.get("url"))
        sources.append({
            "item_id": r.get("item_id"),
            "title": r.get("title"),
            "author": r.get("author_name"),
            "platform": platform,
            "published_at": to_utc_iso(r.get("published_at") or r.get("fetched_at")),
            "url": r.get("url"),
            "cover_url": r.get("cover_url"),
            "media_urls": image_urls(source_media),
            "media": source_media,
            "is_primary_source": int(bool(r.get("is_primary_source"))),
            "authority_badge": badge,
            "snippet": (r.get("snippet") or "").strip(),
        })
    result = {
        "cluster": detail,
        "sources": sources,
        "sources_next_cursor": (page + 1) if has_more else None,
        "data_backend": event_read_backend(),
    }
    return _cache_set_copy_with_ttl(cache_key, result, _cluster_bundle_cache_ttl_sec())


def mark_cluster_clicked(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT live_version FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            live_version = int(row["live_version"] or 0)
            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, clicked_at, last_seen_version
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, now(), %(live_version)s)
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         clicked_at = excluded.clicked_at,
                         last_seen_version = excluded.last_seen_version""",
                {"user_id": user_id, "cluster_id": cluster_id, "live_version": live_version},
            )
        conn.commit()
    clear_user_cache_keys(user_id)
    return {"ok": True, "last_seen_version": live_version, "data_backend": status_backend()}


def get_cluster_statuses(*, user_id: str, cluster_ids: list[int]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(int(value) for value in cluster_ids))
    rows: list[dict[str, Any]] = []
    if ids:
        schema = remote_schema()
        with connect() as conn:
            rows = [dict(row) for row in conn.execute(
                f"""SELECT cluster_id, clicked_at, last_seen_version
                       FROM {schema}.cluster_status
                      WHERE user_id = %(user_id)s
                        AND cluster_id = ANY(%(cluster_ids)s)""",
                {"user_id": user_id, "cluster_ids": ids},
            ).fetchall()]
    status_map = {int(row["cluster_id"]): row for row in rows}
    return [
        {
            "cluster_id": cluster_id,
            "clicked_at": to_utc_iso(status_map[cluster_id].get("clicked_at"))
                if cluster_id in status_map and status_map[cluster_id].get("clicked_at") else None,
            "last_seen_version": int(status_map[cluster_id]["last_seen_version"])
                if cluster_id in status_map and status_map[cluster_id].get("last_seen_version") is not None else None,
        }
        for cluster_id in ids
    ]


def _resolve_highlights_reading_progress(conn: Any, schema: str, user_id: str) -> dict[str, Any] | None:
    saved = conn.execute(
        f"""SELECT cluster_id, anchor_sort_at, updated_at
               FROM {schema}.reading_progress
              WHERE user_id=%(user_id)s AND surface='highlights'""",
        {"user_id": user_id},
    ).fetchone()
    if not saved:
        return None
    display_filter = _highlights_display_cluster_filter(
        schema, "c", threshold=_highlights_display_threshold(),
    )
    resolved = conn.execute(
        f"""WITH eligible AS (
            SELECT h.cluster_id, h.sort_at,
                   row_number() OVER (ORDER BY {_highlights_scope_item_order_sql('h')}) AS rank,
                   h.version_id, h.scope_key
              FROM {schema}.highlights_read_model_state st
              JOIN {schema}.highlights_scope_items h
                ON h.version_id=st.active_version_id AND h.scope_key='all'
              LEFT JOIN {schema}.clusters c ON c.id=h.cluster_id
             WHERE st.key=%(state_key)s
               AND h.sort_at < %(published_before)s
               {display_filter}
            )
            SELECT h.cluster_id, h.sort_at, h.rank,
                   h.version_id::text AS version_id, h.scope_key
              FROM eligible h
             WHERE (h.cluster_id=%(cluster_id)s OR h.sort_at <= %(anchor_sort_at)s)
             ORDER BY CASE WHEN h.cluster_id=%(cluster_id)s THEN 0 ELSE 1 END,
                      h.sort_at DESC NULLS LAST, h.rank ASC
             LIMIT 1""",
        {
            "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
            "cluster_id": int(saved["cluster_id"]),
            "anchor_sort_at": saved["anchor_sort_at"],
            "published_before": highlights_published_before(),
        },
    ).fetchone()
    if not resolved:
        return None
    rank = max(1, int(resolved["rank"] or 1))
    return {
        "cluster_id": int(saved["cluster_id"]),
        "resolved_cluster_id": int(resolved["cluster_id"]),
        "anchor_sort_at": to_utc_iso(saved["anchor_sort_at"]),
        "updated_at": to_utc_iso(saved["updated_at"]),
        "cursor": {
            "version_id": str(resolved["version_id"]),
            "scope_key": str(resolved["scope_key"]),
            "rank_after": ((rank - 1) // 20) * 20,
        },
        "resolution": "exact" if int(resolved["cluster_id"]) == int(saved["cluster_id"]) else "nearest_older",
    }


def save_highlights_reading_progress(*, user_id: str, cluster_id: int) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        anchor = conn.execute(
            f"""SELECT h.sort_at
                  FROM {schema}.highlights_read_model_state st
                  JOIN {schema}.highlights_scope_items h
                    ON h.version_id=st.active_version_id AND h.scope_key='all'
                 WHERE st.key=%(state_key)s AND h.cluster_id=%(cluster_id)s
                 LIMIT 1""",
            {"state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY, "cluster_id": cluster_id},
        ).fetchone()
        if not anchor:
            return None
        conn.execute(
            f"""INSERT INTO {schema}.reading_progress
                   (user_id,surface,cluster_id,anchor_sort_at,updated_at)
                 VALUES (%(user_id)s,'highlights',%(cluster_id)s,%(anchor_sort_at)s,now())
                 ON CONFLICT (user_id,surface) DO UPDATE SET
                   cluster_id=excluded.cluster_id,
                   anchor_sort_at=excluded.anchor_sort_at,
                   updated_at=excluded.updated_at""",
            {"user_id": user_id, "cluster_id": cluster_id, "anchor_sort_at": anchor["sort_at"]},
        )
        conn.commit()
        return _resolve_highlights_reading_progress(conn, schema, user_id)


def get_highlights_reading_progress(*, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        return _resolve_highlights_reading_progress(conn, schema, user_id)


def mark_cluster_seen(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT live_version FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            live_version = int(row["live_version"] or 0)
            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, last_seen_version
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, %(live_version)s)
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         last_seen_version = excluded.last_seen_version""",
                {"user_id": user_id, "cluster_id": cluster_id, "live_version": live_version},
            )
        conn.commit()
    return {
        "cluster_id": cluster_id,
        "last_seen_version": live_version,
        "data_backend": status_backend(),
    }


def set_cluster_feedback(
    *,
    cluster_id: int,
    user_id: str,
    kind: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """v25.0 F-D — per-user cluster 质量反馈，同 kind 再提交=撤销。"""
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT 1 FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            status = cur.execute(
                f"""SELECT feedback_kind
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
            if status and status.get("feedback_kind") == kind:
                cur.execute(
                    f"""UPDATE {schema}.cluster_status
                           SET feedback_kind = NULL,
                               feedback_at = NULL,
                               feedback_note = NULL
                         WHERE user_id = %(user_id)s
                           AND cluster_id = %(cluster_id)s""",
                    {"user_id": user_id, "cluster_id": cluster_id},
                )
                conn.commit()
                clear_user_cache_keys(user_id)
                return {
                    "ok": True,
                    "feedback_kind": None,
                    "feedback_note": None,
                    "data_backend": status_backend(),
                }

            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, feedback_kind, feedback_at, feedback_note
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, %(kind)s, now(), %(note)s)
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         feedback_kind = excluded.feedback_kind,
                         feedback_at = excluded.feedback_at,
                         feedback_note = excluded.feedback_note""",
                {
                    "user_id": user_id,
                    "cluster_id": cluster_id,
                    "kind": kind,
                    "note": note,
                },
            )
        conn.commit()
    clear_user_cache_keys(user_id)
    return {
        "ok": True,
        "feedback_kind": kind,
        "feedback_note": note,
        "data_backend": status_backend(),
    }


def set_cluster_star(*, cluster_id: int, user_id: str) -> dict[str, Any] | None:
    schema = remote_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            row = cur.execute(
                f"SELECT 1 FROM {schema}.clusters WHERE id = %(cluster_id)s",
                {"cluster_id": cluster_id},
            ).fetchone()
            if not row:
                return None
            status = cur.execute(
                f"""SELECT starred_at
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
            if status and status.get("starred_at"):
                cur.execute(
                    f"""UPDATE {schema}.cluster_status
                           SET starred_at = NULL
                         WHERE user_id = %(user_id)s
                           AND cluster_id = %(cluster_id)s""",
                    {"user_id": user_id, "cluster_id": cluster_id},
                )
                conn.commit()
                clear_user_cache_keys(user_id)
                return {"ok": True, "starred_at": None, "data_backend": status_backend()}

            cur.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, starred_at
                       )
                       VALUES (%(user_id)s, %(cluster_id)s, now())
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         starred_at = excluded.starred_at""",
                {"user_id": user_id, "cluster_id": cluster_id},
            )
            starred = cur.execute(
                f"""SELECT starred_at
                      FROM {schema}.cluster_status
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            ).fetchone()
        conn.commit()
    clear_user_cache_keys(user_id)
    return {
        "ok": True,
        "starred_at": to_utc_iso(starred.get("starred_at")) if starred and starred.get("starred_at") else None,
        "data_backend": status_backend(),
    }


def _library_cluster_entry(row: dict[str, Any], *, status_field: str) -> dict[str, Any]:
    viewer_status = {
        "clicked_at": to_utc_iso(row.get("clicked_at")) if row.get("clicked_at") else None,
        "starred_at": to_utc_iso(row.get("starred_at")) if row.get("starred_at") else None,
        "last_seen_version": int(row.get("last_seen_version") or 0),
    }
    cover_url = row.get("cover_url")
    cluster = {
        "id": int(row["id"]),
        "ai_title": row.get("ai_title"),
        "ai_summary": row.get("ai_summary"),
        "why_read": row.get("why_read"),
        "doc_count": int(row.get("doc_count") or 0),
        "unique_source_count": int(row.get("unique_source_count") or 0),
        "platforms": _json_array(row.get("platforms_json")),
        "category": row.get("category"),
        "first_doc_at": to_utc_iso(row.get("first_doc_at")) or row.get("first_doc_at"),
        "last_doc_at": to_utc_iso(row.get("last_doc_at")) if row.get("last_doc_at") else None,
        "cover_url": cover_url,
        "media_urls": [cover_url] if cover_url else [],
        "live_version": int(row.get("live_version") or 0),
        "user_last_seen_version": viewer_status["last_seen_version"],
        "is_visible_in_feed": bool(row.get("is_visible_in_feed")),
        "viewer_status": viewer_status,
        "data_backend": event_read_backend(),
    }
    return {
        "id": f"cluster:{int(row['id'])}",
        "type": "cluster",
        "occurred_at": to_utc_iso(row.get(status_field)) or row.get(status_field),
        "cluster": cluster,
    }
