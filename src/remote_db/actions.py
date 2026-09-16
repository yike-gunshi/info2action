from __future__ import annotations


def _actions_board_result_cache_ttl_sec(env: dict[str, str] | None = None) -> int:
    values = env or _runtime_env()
    return _env_int(values, ACTION_BOARD_RESULT_CACHE_TTL_ENV, 300, min_value=0)


def _remote_actions_board_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_ACTIONS_BOARD_TIMEOUT_MS_ENV, 4500, min_value=500)


def _remote_actions_board_detail_timeout_ms() -> int:
    return _env_int(_runtime_env(), REMOTE_ACTIONS_BOARD_DETAIL_TIMEOUT_MS_ENV, 1200, min_value=300)


def bump_cluster_version_and_stale_actions_remote(
    pg_conn: Any | None,
    cluster_id: int,
    new_version: int,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            bump_cluster_version_and_stale_actions_remote(conn, cluster_id, new_version)
            return
    schema = remote_schema()
    pg_conn.execute(
        f"UPDATE {schema}.clusters SET live_version = %s WHERE id = %s",
        (new_version, cluster_id),
    )
    pg_conn.execute(
        f"""UPDATE {schema}.actions
               SET is_stale = 1
             WHERE source_type = 'cluster'
               AND source_id = %s
               AND (cluster_version IS NULL OR cluster_version < %s)
               AND is_stale = 0""",
        (str(cluster_id), new_version),
    )
    _commit_if_supported(pg_conn)


def _normalize_action_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    if "source_item_ids" in data:
        data["source_item_ids"] = _json_value(data.get("source_item_ids")) or []
    for col in (
        "created_at",
        "confirmed_at",
        "executed_at",
        "completed_at",
        "dismissed_at",
        "dispatched_at",
        "project_context_updated_at",
    ):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def log_action_event_remote(
    pg_conn: Any | None,
    action_id: str,
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            log_action_event_remote(conn, action_id, event_type, detail)
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.action_logs
              (action_id, event_type, detail_json)
            VALUES (%s, %s, %s)""",
        (action_id, event_type, _maybe_jsonb(detail) if detail else None),
    )
    _commit_if_supported(pg_conn)


def create_action_remote(
    pg_conn: Any | None = None,
    *,
    source_type: str,
    title: str,
    action_type: str,
    prompt: str,
    source_item_ids: list[str] | None = None,
    reason: str | None = None,
    priority: str = "medium",
    related_project: str | None = None,
    status: str = "pending",
    direction: str = "_uncategorized",
    direction_label: str = "待归类",
    user_id: str | None = None,
    source_id: str | None = None,
    cluster_version: int | None = None,
    steps: list[str] | None = None,
) -> str:
    if pg_conn is None:
        with connect() as conn:
            return create_action_remote(
                conn,
                source_type=source_type,
                title=title,
                action_type=action_type,
                prompt=prompt,
                source_item_ids=source_item_ids,
                reason=reason,
                priority=priority,
                related_project=related_project,
                status=status,
                direction=direction,
                direction_label=direction_label,
                user_id=user_id,
                source_id=source_id,
                cluster_version=cluster_version,
                steps=steps,
            )
    action_id = str(uuid.uuid4())
    steps_text = json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) and steps else None
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.actions
              (id, user_id, source_type, source_item_ids, source_id,
               cluster_version, original_title, original_prompt,
               original_reason, original_priority, title, action_type,
               related_project, prompt, steps, reason, priority, status,
               direction, direction_label)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            action_id,
            user_id,
            source_type,
            _maybe_jsonb(source_item_ids or []),
            source_id,
            cluster_version,
            title,
            prompt,
            reason,
            priority,
            title,
            action_type,
            related_project,
            prompt,
            steps_text,
            reason,
            priority,
            status,
            direction,
            direction_label,
        ),
    )
    log_action_event_remote(
        pg_conn,
        action_id,
        "created",
        {"source_item_ids": source_item_ids or [], "source_type": source_type},
    )
    _commit_if_supported(pg_conn)
    invalidate_action_board_read_model_remote(pg_conn)
    return action_id


def _action_where(
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    if status == "in_progress":
        where.append("status = ANY(%(statuses)s)")
        params["statuses"] = ["confirmed", "executing", "dispatched"]
    elif status:
        where.append("status = %(status)s")
        params["status"] = status
    if priority:
        where.append("priority = %(priority)s")
        params["priority"] = priority
    if action_type:
        where.append("action_type = %(action_type)s")
        params["action_type"] = action_type
    if direction:
        where.append("direction = %(direction)s")
        params["direction"] = direction
    if source_filter == "with-source":
        where.append("jsonb_array_length(COALESCE(source_item_ids, '[]'::jsonb)) > 0")
    elif source_filter == "no-source":
        where.append("jsonb_array_length(COALESCE(source_item_ids, '[]'::jsonb)) = 0")
    return where, params


_ACTION_BOARD_LANES = (
    {
        "slug": "pending",
        "label": "待处理",
        "statuses": ["pending"],
    },
    {
        "slug": "in_progress",
        "label": "执行中",
        "statuses": ["confirmed", "executing", "dispatched"],
    },
    {
        "slug": "done",
        "label": "已完成",
        "statuses": ["done"],
    },
)
_ACTION_BOARD_VISIBLE_STATUSES = [
    status
    for lane in _ACTION_BOARD_LANES
    for status in lane["statuses"]
]


def _action_board_lane_for_status(status: str | None) -> str | None:
    value = str(status or "")
    for lane in _ACTION_BOARD_LANES:
        if value in lane["statuses"]:
            return str(lane["slug"])
    return None


def _action_board_lanes_for_status(status: str | None) -> list[dict[str, Any]]:
    if not status:
        return [dict(lane) for lane in _ACTION_BOARD_LANES]
    if status == "in_progress":
        return [dict(lane) for lane in _ACTION_BOARD_LANES if lane["slug"] == "in_progress"]
    lane_slug = _action_board_lane_for_status(status)
    if not lane_slug:
        return []
    return [dict(lane) for lane in _ACTION_BOARD_LANES if lane["slug"] == lane_slug]


def _action_date_filter_sql(date_filter: str | None) -> str | None:
    if date_filter == "today":
        return "created_at >= date_trunc('day', now())"
    if date_filter == "week":
        return "created_at >= date_trunc('day', now()) - interval '6 days'"
    return None


def _action_board_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), ACTION_BOARD_READ_MODEL_ENV, default=True)


def _action_board_read_model_refresh_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), ACTION_BOARD_READ_MODEL_REFRESH_ENV, default=True)


def _action_board_read_model_refresh_timeout_ms() -> int:
    return _env_int(
        _runtime_env(),
        ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        ACTION_BOARD_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=5000,
    )


def _action_board_viewer_scope(can_view_all: bool) -> str:
    return "admin" if can_view_all else "owner"


def _action_board_read_model_state_key(viewer_scope: str, owner_user_id: str | None) -> str:
    owner_key = owner_user_id if viewer_scope != "admin" and owner_user_id else "_all"
    return f"{ACTION_BOARD_READ_MODEL_STATE_PREFIX}:{viewer_scope}:{owner_key}"


def _action_board_scope_key(date_filter: str | None, priority: str | None) -> str:
    return f"date:{date_filter or 'all'}|priority:{priority or 'all'}"


def _action_board_read_model_supported(
    *,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    include_detail_payloads: bool,
) -> bool:
    return not any((action_type, direction, source_filter, include_detail_payloads))


def _action_board_read_model_active_version(
    conn: Any,
    schema: str,
    state_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"""SELECT v.version_id,
                   v.viewer_scope,
                   v.owner_user_id,
                   v.payload_version,
                   v.generated_at,
                   v.completed_at,
                   v.max_action_updated_at,
                   v.total_actions,
                   v.meta_json,
                   (v.generated_at < date_trunc('day', now())) AS generated_before_today
              FROM {schema}.action_board_read_model_state st
              JOIN {schema}.action_board_read_model_versions v
                ON v.version_id = st.active_version_id
             WHERE st.key = %(state_key)s
               AND v.status = 'complete'
               AND v.payload_version = %(payload_version)s
             LIMIT 1""",
        {"state_key": state_key, "payload_version": ACTION_BOARD_READ_MODEL_VERSION},
    ).fetchone()
    return dict(row) if row else None


def _action_board_read_model_is_stale(active: dict[str, Any] | None, date_filter: str | None) -> bool:
    if not active:
        return True
    if int(active.get("payload_version") or 0) != ACTION_BOARD_READ_MODEL_VERSION:
        return True
    # The projection prebuilds "today" and "week" scopes. Rebuild once per day
    # even when no actions changed so those relative scopes do not drift at midnight.
    if date_filter in {"today", "week"} and bool(active.get("generated_before_today")):
        return True
    return False


def invalidate_action_board_read_model_remote(pg_conn: Any | None = None) -> None:
    """Invalidate active board projections after an action write.

    Action writes are low-volume, and the board has only a handful of active
    scope keys, so clearing all active state is simpler and safer than trying to
    infer which viewer/date/priority scopes changed.
    """
    if pg_conn is None:
        with connect() as conn:
            invalidate_action_board_read_model_remote(conn)
            return
    try:
        pg_conn.execute(
            f"DELETE FROM {remote_schema()}.action_board_read_model_state WHERE key LIKE %s",
            (f"{ACTION_BOARD_READ_MODEL_STATE_PREFIX}:%",),
        )
        _commit_if_supported(pg_conn)
        try:
            clear_actions_board_cache_keys()
        except Exception:
            pass
    except Exception:
        _rollback_safely(pg_conn)


def refresh_action_board_read_model_remote(
    *,
    owner_user_id: str | None = None,
    can_view_all: bool = False,
) -> dict[str, Any]:
    """Build a complete action board projection for one viewer scope."""
    if not _action_board_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    viewer_scope = _action_board_viewer_scope(can_view_all)
    state_key = _action_board_read_model_state_key(viewer_scope, owner_user_id)
    version_id = str(uuid.uuid4())
    scope_defs = [
        {
            "scope_key": _action_board_scope_key(date_filter, priority),
            "date_filter": date_filter or "all",
            "priority_filter": priority,
            "priority_key": priority or "all",
        }
        for date_filter in (None, "today", "week")
        for priority in (None, "high", "medium", "low", "bug")
    ]
    lane_defs = [dict(lane) for lane in _ACTION_BOARD_LANES]
    where = ["status = ANY(%(visible_statuses)s)"]
    params: dict[str, Any] = {
        "version_id": version_id,
        "state_key": state_key,
        "viewer_scope": viewer_scope,
        "owner_user_id": owner_user_id,
        "payload_version": ACTION_BOARD_READ_MODEL_VERSION,
        "read_model_name": ACTION_BOARD_READ_MODEL_NAME,
        "visible_statuses": _ACTION_BOARD_VISIBLE_STATUSES,
        "scope_defs": json.dumps(scope_defs, ensure_ascii=False),
        "lane_defs": json.dumps(lane_defs, ensure_ascii=False),
    }
    if owner_user_id and viewer_scope != "admin":
        where.append("user_id = %(owner_user_id)s")
    where_sql = _where_sql(where)
    t0 = time.time()
    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, _action_board_read_model_refresh_timeout_ms())
            conn.execute(
                f"""INSERT INTO {schema}.action_board_read_model_versions (
                       version_id, viewer_scope, owner_user_id, payload_version,
                       status, generated_at, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, %(viewer_scope)s, %(owner_user_id)s,
                       %(payload_version)s, 'building', now(),
                       jsonb_build_object('read_model', %(read_model_name)s::text)
                     )""",
                params,
            )
            conn.commit()
            _set_short_statement_timeout(conn, _action_board_read_model_refresh_timeout_ms())
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_base")
            conn.execute(
                f"""CREATE TEMP TABLE action_board_rm_base ON COMMIT DROP AS
                    SELECT id::text AS action_id,
                           source_item_ids,
                           title,
                           action_type,
                           prompt,
                           priority,
                           status,
                           direction,
                           direction_label,
                           created_at,
                           GREATEST(
                             COALESCE(created_at, '-infinity'::timestamptz),
                             COALESCE(confirmed_at, '-infinity'::timestamptz),
                             COALESCE(executed_at, '-infinity'::timestamptz),
                             COALESCE(completed_at, '-infinity'::timestamptz),
                             COALESCE(dismissed_at, '-infinity'::timestamptz),
                             COALESCE(dispatched_at, '-infinity'::timestamptz),
                             COALESCE(project_context_updated_at, '-infinity'::timestamptz)
                           ) AS action_updated_at,
                           CASE
                             WHEN status = 'pending' THEN 'pending'
                             WHEN status IN ('confirmed', 'executing', 'dispatched') THEN 'in_progress'
                             WHEN status = 'done' THEN 'done'
                             ELSE NULL
                           END AS lane_slug,
                           jsonb_strip_nulls(jsonb_build_object(
                             'id', id::text,
                             'source_item_ids', COALESCE(source_item_ids, '[]'::jsonb),
                             'title', title,
                             'action_type', action_type,
                             'prompt', prompt,
                             'priority', priority,
                             'status', status,
                             'direction', direction,
                             'direction_label', direction_label,
                             'created_at', created_at
                           )) AS card_json
                      FROM {schema}.actions
                      {where_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.action_board_rm_base")
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_scope_defs")
            conn.execute(
                """CREATE TEMP TABLE action_board_rm_scope_defs ON COMMIT DROP AS
                   SELECT scope_key, date_filter, priority_filter, priority_key
                     FROM jsonb_to_recordset(%(scope_defs)s::jsonb)
                          AS scope(scope_key text, date_filter text,
                                   priority_filter text, priority_key text)""",
                params,
            )
            conn.execute("DROP TABLE IF EXISTS pg_temp.action_board_rm_scope_rows")
            conn.execute(
                """CREATE TEMP TABLE action_board_rm_scope_rows ON COMMIT DROP AS
                   SELECT sd.scope_key,
                          sd.date_filter,
                          sd.priority_filter,
                          sd.priority_key,
                          b.lane_slug,
                          b.status,
                          b.action_id,
                          b.created_at,
                          b.card_json
                     FROM pg_temp.action_board_rm_scope_defs sd
                     JOIN pg_temp.action_board_rm_base b
                       ON (sd.date_filter = 'all'
                           OR (sd.date_filter = 'today'
                               AND b.created_at >= date_trunc('day', now()))
                           OR (sd.date_filter = 'week'
                               AND b.created_at >= date_trunc('day', now()) - interval '6 days'))
                      AND (sd.priority_filter IS NULL OR b.priority = sd.priority_filter)
                    WHERE b.lane_slug IS NOT NULL"""
            )
            conn.execute("ANALYZE pg_temp.action_board_rm_scope_rows")
            conn.execute(
                f"""WITH scope_totals AS (
                       SELECT scope_key, count(*)::integer AS total_count
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key
                     ),
                     status_counts AS (
                       SELECT scope_key, status, count(*)::integer AS cnt
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key, status
                     ),
                     status_json AS (
                       SELECT scope_key, jsonb_object_agg(status, cnt) AS status_counts_json
                         FROM status_counts
                        GROUP BY scope_key
                     )
                     INSERT INTO {schema}.action_board_scopes (
                       version_id, scope_key, date_filter, priority_filter,
                       total_count, status_counts_json, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            sd.scope_key,
                            sd.date_filter,
                            sd.priority_key,
                            COALESCE(st.total_count, 0),
                            COALESCE(sj.status_counts_json, '{{}}'::jsonb),
                            now()
                       FROM pg_temp.action_board_rm_scope_defs sd
                       LEFT JOIN scope_totals st ON st.scope_key = sd.scope_key
                       LEFT JOIN status_json sj ON sj.scope_key = sd.scope_key""",
                params,
            )
            conn.execute(
                f"""WITH lane_defs AS (
                       SELECT slug, label
                         FROM jsonb_to_recordset(%(lane_defs)s::jsonb)
                              AS lane(slug text, label text, statuses jsonb)
                     ),
                     lane_counts AS (
                       SELECT scope_key, lane_slug, count(*)::integer AS total_count
                         FROM pg_temp.action_board_rm_scope_rows
                        GROUP BY scope_key, lane_slug
                     )
                     INSERT INTO {schema}.action_board_scope_lanes (
                       version_id, scope_key, lane_slug, lane_label,
                       total_count, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            sd.scope_key,
                            lane_defs.slug,
                            lane_defs.label,
                            COALESCE(lc.total_count, 0),
                            now()
                       FROM pg_temp.action_board_rm_scope_defs sd
                       CROSS JOIN lane_defs
                       LEFT JOIN lane_counts lc
                         ON lc.scope_key = sd.scope_key
                        AND lc.lane_slug = lane_defs.slug""",
                params,
            )
            conn.execute(
                f"""WITH ranked AS (
                       SELECT scope_key,
                              lane_slug,
                              action_id,
                              created_at,
                              card_json,
                              row_number() OVER (
                                PARTITION BY scope_key, lane_slug
                                ORDER BY created_at DESC NULLS LAST, action_id DESC
                              ) AS rn
                         FROM pg_temp.action_board_rm_scope_rows
                     )
                     INSERT INTO {schema}.action_board_scope_items (
                       version_id, scope_key, lane_slug, rank,
                       action_id, created_at, card_json
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            lane_slug,
                            rn::integer,
                            action_id,
                            created_at,
                            card_json
                       FROM ranked""",
                params,
            )
            version_stats = conn.execute(
                """SELECT count(*)::integer AS total_actions,
                          max(action_updated_at) AS max_action_updated_at
                     FROM pg_temp.action_board_rm_base"""
            ).fetchone()
            conn.execute(
                f"""UPDATE {schema}.action_board_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_action_updated_at = %(max_action_updated_at)s,
                           total_actions = %(total_actions)s,
                           meta_json = meta_json || jsonb_build_object(
                             'elapsed_ms', %(elapsed_ms)s::integer,
                             'scope_count', %(scope_count)s::integer
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                {
                    **params,
                    "max_action_updated_at": (version_stats or {}).get("max_action_updated_at"),
                    "total_actions": int((version_stats or {}).get("total_actions") or 0),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "scope_count": len(scope_defs),
                },
            )
            conn.execute(
                f"""INSERT INTO {schema}.action_board_read_model_state (
                       key, active_version_id, updated_at
                     )
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                params,
            )
            scope_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.action_board_scope_items
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            conn.execute(
                f"""DELETE FROM {schema}.action_board_read_model_versions v
                     WHERE v.viewer_scope = %(viewer_scope)s
                       AND v.owner_user_id IS NOT DISTINCT FROM %(owner_user_id)s
                       AND NOT EXISTS (
                         SELECT 1 FROM {schema}.action_board_read_model_state st
                          WHERE st.active_version_id = v.version_id
                       )
                       AND v.version_id NOT IN (
                         SELECT version_id
                           FROM {schema}.action_board_read_model_versions
                          WHERE viewer_scope = %(viewer_scope)s
                            AND owner_user_id IS NOT DISTINCT FROM %(owner_user_id)s
                          ORDER BY generated_at DESC
                          LIMIT 3
                       )""",
                params,
            )
            conn.commit()
        except Exception as exc:
            _rollback_safely(conn)
            try:
                conn.execute(
                    f"""UPDATE {schema}.action_board_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": str(exc)[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError("action board read model refresh failed") from exc
    return {
        "ok": True,
        "version_id": version_id,
        "viewer_scope": viewer_scope,
        "owner_user_id": owner_user_id,
        "scope_items": int((scope_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


def _query_actions_board_read_model_remote(
    *,
    status: str | None,
    priority: str | None,
    action_type: str | None,
    direction: str | None,
    source_filter: str | None,
    date_filter: str | None,
    user_id: str | None,
    can_view_all: bool,
    limit_per_direction: int,
    offset: int,
    include_detail_payloads: bool,
) -> dict[str, Any] | None:
    if not _action_board_read_model_enabled():
        return None
    if not _action_board_read_model_supported(
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        include_detail_payloads=include_detail_payloads,
    ):
        return None
    lane_defs = _action_board_lanes_for_status(status)
    if not lane_defs:
        return None
    limit = max(1, min(int(limit_per_direction or 20), 50))
    start = max(0, int(offset or 0))
    schema = remote_schema()
    viewer_scope = _action_board_viewer_scope(can_view_all)
    state_key = _action_board_read_model_state_key(viewer_scope, user_id)
    scope_key = _action_board_scope_key(date_filter, priority)
    lane_slugs = [str(lane["slug"]) for lane in lane_defs]

    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
            active = _action_board_read_model_active_version(conn, schema, state_key)
    except Exception as exc:
        print(f"[warn] action board read model unavailable: {exc}")
        return None

    if _action_board_read_model_is_stale(active, date_filter):
        if not _action_board_read_model_refresh_enabled():
            return None
        try:
            refresh_action_board_read_model_remote(
                owner_user_id=user_id,
                can_view_all=can_view_all,
            )
            with connect() as conn:
                _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
                active = _action_board_read_model_active_version(conn, schema, state_key)
        except Exception as exc:
            print(f"[warn] action board read model refresh degraded to live query: {exc}")
            return None

    if not active or not active.get("version_id"):
        return None

    version_id = str(active["version_id"])
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
            scope_row = conn.execute(
                f"""SELECT scope_key, total_count, status_counts_json
                      FROM {schema}.action_board_scopes
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                     LIMIT 1""",
                {"version_id": version_id, "scope_key": scope_key},
            ).fetchone()
            if not scope_row:
                return None
            lane_rows = conn.execute(
                f"""SELECT lane_slug, lane_label, total_count
                      FROM {schema}.action_board_scope_lanes
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                       AND lane_slug = ANY(%(lane_slugs)s)
                     ORDER BY CASE lane_slug
                                WHEN 'pending' THEN 0
                                WHEN 'in_progress' THEN 1
                                WHEN 'done' THEN 2
                                ELSE 3
                              END""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "lane_slugs": lane_slugs,
                },
            ).fetchall()
            item_rows = conn.execute(
                f"""SELECT lane_slug, rank, action_id, created_at, card_json
                      FROM {schema}.action_board_scope_items
                     WHERE version_id = %(version_id)s::uuid
                       AND scope_key = %(scope_key)s
                       AND lane_slug = ANY(%(lane_slugs)s)
                       AND rank > %(start)s
                       AND rank <= %(end)s
                     ORDER BY CASE lane_slug
                                WHEN 'pending' THEN 0
                                WHEN 'in_progress' THEN 1
                                WHEN 'done' THEN 2
                                ELSE 3
                              END,
                              rank ASC""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "lane_slugs": lane_slugs,
                    "start": start,
                    "end": start + limit,
                },
            ).fetchall()
    except Exception as exc:
        print(f"[warn] action board read model query degraded to live query: {exc}")
        return None

    items_by_lane: dict[str, list[dict[str, Any]]] = {slug: [] for slug in lane_slugs}
    for row in item_rows:
        card = _json_value(row.get("card_json"))
        if not isinstance(card, dict):
            continue
        action = _normalize_action_row(card)
        items_by_lane.setdefault(str(row.get("lane_slug")), []).append(action)

    lane_counts = {str(row.get("lane_slug")): int(row.get("total_count") or 0) for row in lane_rows}
    lane_labels = {str(row.get("lane_slug")): str(row.get("lane_label") or row.get("lane_slug")) for row in lane_rows}
    directions: list[dict[str, Any]] = []
    for lane in lane_defs:
        slug = str(lane["slug"])
        items = items_by_lane.get(slug, [])
        total = int(lane_counts.get(slug, 0) or 0)
        loaded_until = start + len(items)
        has_more = total > loaded_until
        directions.append({
            "slug": slug,
            "label": lane_labels.get(slug) or str(lane["label"]),
            "count": total,
            "items": items,
            "has_more": has_more,
            "next_offset": loaded_until if has_more else None,
        })

    raw_counts = _json_value(scope_row.get("status_counts_json"))
    counts = {status_value: 0 for status_value in _ACTION_BOARD_VISIBLE_STATUSES}
    if isinstance(raw_counts, dict):
        counts.update({str(key): int(value or 0) for key, value in raw_counts.items()})
    counts["in_progress"] = sum(
        int(counts.get(status_value, 0) or 0)
        for status_value in ("confirmed", "executing", "dispatched")
    )
    counts["total"] = int(scope_row.get("total_count") or 0)
    return {
        "counts": counts,
        "directions": directions,
        "meta": {
            "limit_per_direction": limit,
            "offset": start,
            "degraded": False,
            "detail_degraded": False,
            "detail_included": False,
            "read_model": ACTION_BOARD_READ_MODEL_NAME,
            "read_model_version_id": version_id,
            "scope_key": scope_key,
            "query_strategy": "action_board_read_model",
        },
    }


def get_actions_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    where_sql = _where_sql(where)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT *
                  FROM {remote_schema()}.actions
                  {where_sql}
                 ORDER BY
                   CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                               WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                               WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                               WHEN 'dismissed' THEN 4 ELSE 5 END,
                   CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                   created_at DESC""",
            params,
        ).fetchall()
    return [_normalize_action_row(row) for row in rows]


def _action_source_ids(value: Any) -> list[str]:
    data = _json_value(value)
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


_ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS = (
    "created_at",
    "confirmed_at",
    "executed_at",
    "completed_at",
    "dismissed_at",
    "dispatched_at",
    "project_context_updated_at",
)


def _action_source_updated_at(action: dict[str, Any] | None) -> str | None:
    if not action:
        return None
    values = [
        action.get(field)
        for field in _ACTION_DETAIL_SOURCE_TIMESTAMP_FIELDS
        if action.get(field)
    ]
    if not values:
        return None
    return _timestamp_value(max(values, key=sort_key))


def _action_detail_read_model_fresh(row: dict[str, Any]) -> bool:
    source_updated_at = _action_source_updated_at(row)
    if not source_updated_at:
        return True
    cached_source_updated_at = row.get("source_updated_at")
    if not cached_source_updated_at:
        return False
    return sort_key(cached_source_updated_at) >= sort_key(source_updated_at)


def _action_source_items_from_map(
    by_id: dict[str, dict[str, Any]],
    source_ids: list[str],
    *,
    request_user_id: str | None,
    can_view_all: bool,
) -> list[dict[str, Any]]:
    out = []
    for sid in source_ids:
        item = copy.deepcopy(by_id.get(sid))
        if not item:
            continue
        if (
            item.get("platform") == "manual"
            and not can_view_all
            and item.get("user_id") != request_user_id
        ):
            continue
        detail = _json_value(item.get("detail_json")) or {}
        item["referenced_urls"] = detail.get("referenced_urls", []) if isinstance(detail, dict) else []
        item.pop("detail_json", None)
        item.pop("user_id", None)
        out.append(item)
    return out


def get_actions_payload_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    user_id: str | None = None,
    request_user_id: str | None = None,
    can_view_all: bool = False,
    include_source_items: bool = False,
    include_detail_payloads: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    page_limit = max(1, min(int(limit or 100), 200))
    page_offset = max(0, int(offset or 0))
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    where_sql = _where_sql(where)
    page_params = {**params, "limit": page_limit, "offset": page_offset}
    scope_where = []
    scope_params = {}
    if user_id:
        scope_where.append("user_id = %(user_id)s")
        scope_params["user_id"] = user_id
    direction_where = ["status IN ('pending','confirmed','executing','dispatched')", *scope_where]
    with connect() as conn:
        _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
        action_rows = conn.execute(
            f"""SELECT id, source_item_ids, title, action_type, prompt,
                      priority, status, direction, direction_label, created_at
                  FROM {remote_schema()}.actions
                  {where_sql}
                 ORDER BY
                   CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                               WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                               WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                               WHEN 'dismissed' THEN 4 ELSE 5 END,
                   CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                 WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                   created_at DESC
                 LIMIT %(limit)s OFFSET %(offset)s""",
            page_params,
        ).fetchall()
        count_rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(scope_where)}
                 GROUP BY status""",
            scope_params,
        ).fetchall()
        direction_rows = conn.execute(
            f"""SELECT direction, direction_label, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(direction_where)}
                 GROUP BY direction, direction_label
                 ORDER BY cnt DESC""",
            scope_params,
        ).fetchall()
        actions = [_normalize_action_row(row) for row in action_rows]
        if include_source_items:
            source_ids = list(dict.fromkeys(
                sid
                for action in actions
                for sid in _action_source_ids(action.get("source_item_ids"))
            ))
            source_by_id: dict[str, dict[str, Any]] = {}
            if source_ids:
                placeholders = ", ".join(["%s"] * len(source_ids))
                source_rows = conn.execute(
                    f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                          FROM {remote_schema()}.items
                         WHERE id IN ({placeholders})""",
                    tuple(source_ids),
                ).fetchall()
                source_by_id = _source_item_by_id(source_rows)
            for action in actions:
                action_ids = _action_source_ids(action.get("source_item_ids"))
                source_items = _action_source_items_from_map(
                    source_by_id,
                    action_ids,
                    request_user_id=request_user_id,
                    can_view_all=can_view_all,
                )
                action["source_item_ids"] = action_ids
                action["source_items"] = source_items
                action["source_item_count"] = len(source_items)
    if include_detail_payloads:
        viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
        detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(actions)
        detail_payloads = get_action_detail_read_models_remote(
            detail_action_ids,
            viewer_scope=viewer_scope,
            owner_user_id=user_id,
        )
        actions = [
            action_detail_read_model.merge_action_with_detail_payload(
                action,
                detail_payloads.get(str(action.get("id"))),
            )
            for action in actions
        ]
    return {
        "actions": actions,
        "counts": {row["status"]: row["cnt"] for row in count_rows},
        "directions": [
            {"slug": row["direction"], "label": row["direction_label"], "count": row["cnt"]}
            for row in direction_rows
        ],
        "meta": {
            "limit": page_limit,
            "offset": page_offset,
            "degraded": False,
            "query_strategy": "legacy_actions_paginated",
        },
    }


def get_actions_board_payload_remote(
    *,
    status: str | None = None,
    priority: str | None = None,
    action_type: str | None = None,
    direction: str | None = None,
    source_filter: str | None = None,
    date_filter: str | None = None,
    user_id: str | None = None,
    request_user_id: str | None = None,
    can_view_all: bool = False,
    limit_per_direction: int = 20,
    offset: int = 0,
    include_detail_payloads: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(int(limit_per_direction or 20), 50))
    start = max(0, int(offset or 0))
    schema = remote_schema()
    cache_ttl = _actions_board_result_cache_ttl_sec()
    cache_key = (
        "actions_board_result",
        schema,
        status,
        priority,
        action_type,
        direction,
        source_filter,
        date_filter,
        user_id or "",
        bool(can_view_all),
        limit,
        start,
        bool(include_detail_payloads),
    )
    cached = _cache_get_copy_with_ttl(cache_key, cache_ttl)
    if cached is not None:
        return cached
    read_model_payload = _query_actions_board_read_model_remote(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        date_filter=date_filter,
        user_id=user_id,
        can_view_all=can_view_all,
        limit_per_direction=limit,
        offset=start,
        include_detail_payloads=include_detail_payloads,
    )
    if read_model_payload is not None:
        _cache_set_copy_with_ttl(cache_key, read_model_payload, cache_ttl)
        return read_model_payload
    where, params = _action_where(
        status=status,
        priority=priority,
        action_type=action_type,
        direction=direction,
        source_filter=source_filter,
        user_id=user_id,
    )
    lane_defs = _action_board_lanes_for_status(status)
    if not lane_defs:
        return {
            "counts": {"total": 0, "in_progress": 0},
            "directions": [],
            "meta": {
                "limit_per_direction": limit,
                "offset": start,
                "degraded": False,
                "detail_degraded": False,
                "detail_included": False,
                "read_model": False,
                "query_strategy": "status_lanes_lateral",
            },
        }
    if status is None:
        where.append("status = ANY(%(board_statuses)s)")
        params["board_statuses"] = _ACTION_BOARD_VISIBLE_STATUSES
    date_sql = _action_date_filter_sql(date_filter)
    if date_sql:
        where.append(date_sql)
    where_sql = _where_sql(where)

    detail_degraded = False
    with connect() as conn:
        _set_short_statement_timeout(conn, _remote_actions_board_timeout_ms())
        count_rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {schema}.actions
                  {where_sql}
                 GROUP BY status""",
            params,
        ).fetchall()

        raw_counts = {str(row.get("status")): int(row.get("cnt") or 0) for row in count_rows}
        lane_summaries = [
            {
                "slug": lane["slug"],
                "label": lane["label"],
                "statuses": lane["statuses"],
                "cnt": sum(raw_counts.get(status, 0) for status in lane["statuses"]),
            }
            for lane in lane_defs
        ]

        item_where_sql = _where_sql([
            *where,
            "status IN (SELECT jsonb_array_elements_text(lane_summary.statuses))",
        ])
        board_rows = []
        if lane_summaries:
            board_rows = conn.execute(
                f"""WITH lane_summary AS (
                       SELECT slug, label, statuses, cnt
                         FROM jsonb_to_recordset(%(lane_summaries)s::jsonb)
                              AS lane(slug text, label text, statuses jsonb, cnt integer)
                     )
                     SELECT lane_summary.slug AS board_lane,
                            lane_summary.label AS board_lane_label,
                            lane_summary.cnt AS lane_total,
                            a.id, a.source_item_ids, a.title, a.action_type, a.prompt,
                            a.priority, a.status, a.direction, a.direction_label, a.created_at
                       FROM lane_summary
                       LEFT JOIN LATERAL (
                         SELECT id, source_item_ids, title, action_type, prompt,
                                priority, status, direction, direction_label, created_at
                           FROM {schema}.actions
                           {item_where_sql}
                          ORDER BY created_at DESC
                          LIMIT %(limit)s OFFSET %(offset)s
                       ) a ON true
                      ORDER BY CASE lane_summary.slug
                                 WHEN 'pending' THEN 0
                                 WHEN 'in_progress' THEN 1
                                 WHEN 'done' THEN 2
                                 ELSE 3
                               END,
                               a.created_at DESC""",
                {
                    **params,
                    "lane_summaries": json.dumps(lane_summaries, ensure_ascii=False),
                    "limit": limit,
                    "offset": start,
                },
            ).fetchall()

        directions: list[dict[str, Any]] = []
        visible_actions: list[dict[str, Any]] = []
        directions_by_slug: dict[str, dict[str, Any]] = {}
        action_cols = (
            "id",
            "source_item_ids",
            "title",
            "action_type",
            "prompt",
            "priority",
            "status",
            "direction",
            "direction_label",
            "created_at",
        )
        for row in board_rows:
            slug = str(row.get("board_lane") or "")
            label = str(row.get("board_lane_label") or slug)
            total = int(row.get("lane_total") or 0)
            entry = directions_by_slug.setdefault(
                slug,
                {
                    "slug": slug,
                    "label": label,
                    "count": total,
                    "items": [],
                },
            )
            if row.get("id") is None:
                continue
            action = _normalize_action_row({col: row.get(col) for col in action_cols})
            entry["items"].append(action)
            visible_actions.append(action)

        directions = list(directions_by_slug.values())
        for entry in directions:
            loaded_until = start + len(entry["items"])
            entry["has_more"] = int(entry["count"] or 0) > loaded_until
            entry["next_offset"] = loaded_until if entry["has_more"] else None

        if include_detail_payloads and visible_actions:
            viewer_scope = action_detail_read_model.viewer_scope_for(can_view_all=can_view_all)
            detail_action_ids = action_detail_read_model.select_list_prefetch_action_ids(visible_actions)
            try:
                detail_payloads = _get_action_list_detail_payloads_remote(
                    conn,
                    detail_action_ids,
                    viewer_scope=viewer_scope,
                    owner_user_id=user_id,
                    statement_timeout_ms=_remote_actions_board_detail_timeout_ms(),
                )
                merged_by_id = {
                    str(action.get("id")): action_detail_read_model.merge_action_with_detail_payload(
                        action,
                        detail_payloads.get(str(action.get("id"))),
                    )
                    for action in visible_actions
                }
                for entry in directions:
                    entry["items"] = [
                        merged_by_id.get(str(action.get("id")), action)
                        for action in entry["items"]
                    ]
            except Exception as exc:
                detail_degraded = True
                _rollback_safely(conn)
                print(f"[warn] actions board detail payload degraded: {exc}")

    counts = {status: 0 for status in _ACTION_BOARD_VISIBLE_STATUSES}
    counts.update({str(row["status"]): int(row["cnt"] or 0) for row in count_rows})
    counts["in_progress"] = sum(
        int(counts.get(status, 0) or 0)
        for status in ("confirmed", "executing", "dispatched")
    )
    counts["total"] = sum(int(v or 0) for v in counts.values())
    counts["total"] -= int(counts.get("in_progress", 0) or 0)
    return {
        "counts": counts,
        "directions": directions,
        "meta": {
            "limit_per_direction": limit,
            "offset": start,
            "degraded": False,
            "detail_degraded": detail_degraded,
            "detail_included": bool(include_detail_payloads and not detail_degraded),
            "read_model": False,
            "query_strategy": "status_lanes_lateral",
        },
    }


def _get_action_list_detail_payloads_remote(
    conn: Any,
    action_ids: list[str],
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    statement_timeout_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(action_id) for action_id in action_ids if action_id))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    where = [
        f"action_id IN ({placeholders})",
        "viewer_scope = %s",
        "payload_version = %s",
    ]
    params: list[Any] = [
        *ids,
        viewer_scope,
        action_detail_read_model.READ_MODEL_VERSION,
    ]
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %s")
        params.append(owner_user_id)
    if statement_timeout_ms is not None:
        _set_short_statement_timeout(conn, statement_timeout_ms)
    rows = conn.execute(
        f"""SELECT action_id,
                   jsonb_strip_nulls(jsonb_build_object(
                     'steps', payload->'steps',
                     'source_items', payload->'source_items',
                     'source_item_count', payload->'source_item_count',
                     'execution_status', payload->'execution_status',
                     '_list_payload', true
                   )) AS payload
              FROM {remote_schema()}.action_detail_read_models
             WHERE {" AND ".join(where)}""",
        tuple(params),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _json_value(row.get("payload"))
        if isinstance(payload, dict):
            out[str(row.get("action_id"))] = payload
    return out


def get_action_remote(action_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    where = ["id = %(action_id)s"]
    params = {"action_id": action_id}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.actions {_where_sql(where)}",
            params,
        ).fetchone()
    return _normalize_action_row(row) if row else None


def update_action_remote(
    action_id: str,
    *,
    owner_user_id: str | None = None,
    pg_conn: Any | None = None,
    **fields: Any,
) -> bool:
    allowed = {
        "title", "prompt", "reason", "priority", "status", "action_type",
        "related_project", "source_item_ids", "direction", "direction_label",
        "execution_tool", "execution_result", "execution_exit_code",
        "execution_model", "execution_duration_seconds", "session_id",
        "project_context", "project_context_updated_at", "confirmed_at",
        "executed_at", "completed_at", "dismissed_at", "discord_thread_id",
        "discord_thread_url", "dispatched_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if pg_conn is None:
        with connect() as conn:
            return update_action_remote(
                action_id,
                owner_user_id=owner_user_id,
                pg_conn=conn,
                **updates,
            )
    sets = []
    params: dict[str, Any] = {"action_id": action_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        params[pname] = _maybe_jsonb(value) if key == "source_item_ids" else value
    where = "id = %(action_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    cur = pg_conn.execute(
        f"UPDATE {remote_schema()}.actions SET {', '.join(sets)} WHERE {where}",
        params,
    )
    _commit_if_supported(pg_conn)
    invalidate_action_board_read_model_remote(pg_conn)
    return (getattr(cur, "rowcount", 0) or 0) > 0


def delete_action_remote(action_id: str, *, owner_user_id: str | None = None) -> bool:
    if owner_user_id and not get_action_remote(action_id, user_id=owner_user_id):
        return False
    with connect() as conn:
        params = {"action_id": action_id}
        conn.execute(
            f"DELETE FROM {remote_schema()}.action_feedback WHERE action_id = %(action_id)s",
            params,
        )
        conn.execute(
            f"DELETE FROM {remote_schema()}.action_logs WHERE action_id = %(action_id)s",
            params,
        )
        where = "id = %(action_id)s"
        if owner_user_id:
            where += " AND user_id = %(owner_user_id)s"
            params["owner_user_id"] = owner_user_id
        cur = conn.execute(f"DELETE FROM {remote_schema()}.actions WHERE {where}", params)
        conn.commit()
        invalidate_action_board_read_model_remote(conn)
        return (getattr(cur, "rowcount", 0) or 0) > 0


def get_action_counts_remote(*, user_id: str | None = None) -> dict[str, int]:
    where = []
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT status, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(where)}
                 GROUP BY status""",
            params,
        ).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


def get_action_directions_remote(*, user_id: str | None = None) -> list[dict[str, Any]]:
    where = ["status IN ('pending','confirmed','executing','dispatched')"]
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT direction, direction_label, COUNT(*) AS cnt
                  FROM {remote_schema()}.actions
                  {_where_sql(where)}
                 GROUP BY direction, direction_label
                 ORDER BY cnt DESC""",
            params,
        ).fetchall()
    return [
        {"slug": row["direction"], "label": row["direction_label"], "count": row["cnt"]}
        for row in rows
    ]


def get_actions_by_item_remote(item_id: str, *, user_id: str | None = None) -> list[dict[str, Any]]:
    where = ["source_item_ids @> %(source_item_ids)s"]
    params: dict[str, Any] = {"source_item_ids": _maybe_jsonb([item_id])}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, action_type, priority, status, reason, steps,
                      created_at, source_item_ids
                 FROM {remote_schema()}.actions
                 {_where_sql(where)}
                ORDER BY created_at DESC""",
            params,
        ).fetchall()
    result = []
    for row in rows:
        d = _normalize_action_row(row)
        # v2 §14.3(T7): steps 供信息弹窗列表原位展示
        raw_steps = d.get('steps')
        if isinstance(raw_steps, str):
            try:
                d['steps'] = json.loads(raw_steps)
            except (json.JSONDecodeError, TypeError):
                d['steps'] = None
        result.append(d)
    return result


def get_cluster_actions_remote(cluster_id: int, *, user_id: str) -> list[dict[str, Any]]:
    """BF-0706-3: 事件弹窗行动列表的 remote 分支(镜像 clusters.cluster_actions_list 本地查询)。

    Why: cluster_actions_list 之前只读本地 sqlite,生产走 Supabase 时事件下已生成行动
    点一个都不显示。按 source_type='cluster' AND source_id AND user_id 查。
    """
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, action_type, prompt, priority, status,
                      cluster_version, is_stale, created_at
                 FROM {remote_schema()}.actions
                WHERE source_type = 'cluster'
                  AND source_id = %(source_id)s
                  AND user_id = %(user_id)s
                ORDER BY created_at DESC""",
            {"source_id": str(cluster_id), "user_id": user_id},
        ).fetchall()
    return [_normalize_action_row(row) for row in rows]


def add_action_feedback_remote(action_id: str, phase: str, rating: str, comment: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.action_feedback
                  (action_id, phase, rating, comment)
                VALUES (%s, %s, %s, %s)""",
            (action_id, phase, rating, comment),
        )
        log_action_event_remote(
            conn,
            action_id,
            "feedback",
            {"phase": phase, "rating": rating, "comment": comment},
        )
        conn.commit()


def get_item_action_context_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, platform, title, content, ai_summary, ai_key_points,
                      ai_category, detail_json
                 FROM {remote_schema()}.items
                WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["detail_json"] = _json_value(item.get("detail_json"))
    return item


def get_action_source_items_remote(
    source_ids: list[str],
    *,
    request_user_id: str | None,
    can_view_all: bool,
) -> list[dict[str, Any]]:
    if not source_ids:
        return []
    placeholders = ", ".join(["%s"] * len(source_ids))
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                  FROM {remote_schema()}.items
                 WHERE id IN ({placeholders})""",
            tuple(source_ids),
        ).fetchall()
    return _action_source_items_from_map(
        _source_item_by_id(rows),
        source_ids,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
    )


def get_action_detail_read_model_remote(
    action_id: str,
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
) -> dict[str, Any] | None:
    where = [
        "action_id = %(action_id)s",
        "viewer_scope = %(viewer_scope)s",
        "payload_version = %(payload_version)s",
    ]
    params: dict[str, Any] = {
        "action_id": action_id,
        "viewer_scope": viewer_scope,
        "payload_version": action_detail_read_model.READ_MODEL_VERSION,
    }
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %(owner_user_id)s")
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        row = conn.execute(
            f"""SELECT rm.payload, rm.source_updated_at,
                       a.created_at, a.confirmed_at, a.executed_at,
                       a.completed_at, a.dismissed_at, a.dispatched_at,
                       a.project_context_updated_at
                  FROM {remote_schema()}.action_detail_read_models rm
                  JOIN {remote_schema()}.actions a
                    ON a.id = rm.action_id
                 {_where_sql([clause.replace("action_id", "rm.action_id", 1) for clause in where])}
                 LIMIT 1""",
            params,
        ).fetchone()
    if not row:
        return None
    row_data = dict(row)
    if not _action_detail_read_model_fresh(row_data):
        return None
    payload = _json_value(row.get("payload"))
    return payload if isinstance(payload, dict) else None


def get_action_detail_read_models_remote(
    action_ids: list[str],
    *,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    statement_timeout_ms: int | None = None,
) -> dict[str, dict[str, Any]]:
    ids = list(dict.fromkeys(str(action_id) for action_id in action_ids if action_id))
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    where = [
        f"action_id IN ({placeholders})",
        "viewer_scope = %s",
        "payload_version = %s",
    ]
    params: list[Any] = [
        *ids,
        viewer_scope,
        action_detail_read_model.READ_MODEL_VERSION,
    ]
    if owner_user_id and viewer_scope != "admin":
        where.append("owner_user_id = %s")
        params.append(owner_user_id)
    with connect() as conn:
        if statement_timeout_ms is not None:
            _set_short_statement_timeout(conn, statement_timeout_ms)
        rows = conn.execute(
            f"""SELECT rm.action_id, rm.payload, rm.source_updated_at,
                       a.created_at, a.confirmed_at, a.executed_at,
                       a.completed_at, a.dismissed_at, a.dispatched_at,
                       a.project_context_updated_at
                  FROM {remote_schema()}.action_detail_read_models rm
                  JOIN {remote_schema()}.actions a
                    ON a.id = rm.action_id
                 WHERE {" AND ".join(clause.replace("action_id", "rm.action_id", 1) for clause in where)}""",
            tuple(params),
        ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_data = dict(row)
        if not _action_detail_read_model_fresh(row_data):
            continue
        payload = _json_value(row.get("payload"))
        if isinstance(payload, dict):
            out[str(row.get("action_id"))] = payload
    return out


def action_detail_read_model_freshness_remote(
    *,
    viewer_scope: str = "admin",
    limit: int | None = None,
) -> dict[str, Any]:
    """Read-only health probe for action detail read-model payloads used by Action Tab."""
    safe_limit = max(1, min(int(limit or action_detail_read_model.LIST_PREFETCH_TOTAL), 100))
    schema = remote_schema()
    try:
        with connect() as conn:
            _set_short_statement_timeout(conn, 2500)
            latest_row = conn.execute(
                f"""SELECT created_at
                      FROM {schema}.actions
                     ORDER BY created_at DESC
                     LIMIT 1"""
            ).fetchone()
            rows = conn.execute(
                f"""WITH top_actions AS (
                       SELECT id, created_at, confirmed_at, executed_at,
                              completed_at, dismissed_at, dispatched_at,
                              project_context_updated_at
                         FROM {schema}.actions
                        ORDER BY
                          CASE status WHEN 'dispatched' THEN 0 WHEN 'executing' THEN 0
                                      WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1
                                      WHEN 'done' THEN 2 WHEN 'failed' THEN 3
                                      WHEN 'dismissed' THEN 4 ELSE 5 END,
                          CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1
                                        WHEN 'low' THEN 2 WHEN 'bug' THEN 3 ELSE 4 END,
                          created_at DESC
                        LIMIT %(limit)s
                     )
                     SELECT a.id, a.created_at, a.confirmed_at, a.executed_at,
                            a.completed_at, a.dismissed_at, a.dispatched_at,
                            a.project_context_updated_at, rm.source_updated_at
                       FROM top_actions a
                       LEFT JOIN {schema}.action_detail_read_models rm
                         ON rm.action_id = a.id
                        AND rm.viewer_scope = %(viewer_scope)s
                        AND rm.payload_version = %(payload_version)s
                      ORDER BY
                        CASE WHEN rm.action_id IS NULL THEN 1 ELSE 0 END DESC,
                        a.created_at DESC""",
                {
                    "limit": safe_limit,
                    "viewer_scope": viewer_scope,
                    "payload_version": action_detail_read_model.READ_MODEL_VERSION,
                },
            ).fetchall()
    except Exception as exc:
        raise RemoteDBError(f"action detail read model freshness probe failed: {exc}") from exc

    top_count = len(rows)
    missing = 0
    stale = 0
    stale_action_ids: list[str] = []
    for row in rows:
        data = dict(row)
        if not data.get("source_updated_at"):
            missing += 1
            stale_action_ids.append(str(data.get("id")))
            continue
        if not _action_detail_read_model_fresh(data):
            stale += 1
            stale_action_ids.append(str(data.get("id")))
    return {
        "enabled": True,
        "read_model": "action_detail_v1",
        "payload_version": action_detail_read_model.READ_MODEL_VERSION,
        "viewer_scope": viewer_scope,
        "data_backend": feed_read_backend(),
        "latest_action_created_at": _timestamp_value((latest_row or {}).get("created_at")),
        "sample_limit": safe_limit,
        "sampled_actions": top_count,
        "prefetch_missing_count": missing,
        "prefetch_stale_count": stale,
        "prefetch_unfresh_count": missing + stale,
        "stale_action_ids_sample": stale_action_ids[:10],
        "stale": bool(missing or stale),
    }


def upsert_action_detail_read_model_remote(
    *,
    action_id: str,
    viewer_scope: str = "owner",
    owner_user_id: str | None = None,
    payload: dict[str, Any],
    source_item_ids: list[str] | None = None,
    source_updated_at: str | None = None,
    pg_conn: Any | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            upsert_action_detail_read_model_remote(
                action_id=action_id,
                viewer_scope=viewer_scope,
                owner_user_id=owner_user_id,
                payload=payload,
                source_item_ids=source_item_ids,
                source_updated_at=source_updated_at,
                pg_conn=conn,
            )
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.action_detail_read_models
              (action_id, viewer_scope, owner_user_id, payload, source_item_ids,
               payload_version, built_at, source_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
            ON CONFLICT (action_id, viewer_scope) DO UPDATE SET
              owner_user_id = excluded.owner_user_id,
              payload = excluded.payload,
              source_item_ids = excluded.source_item_ids,
              payload_version = excluded.payload_version,
              built_at = excluded.built_at,
              source_updated_at = excluded.source_updated_at""",
        (
            action_id,
            viewer_scope,
            owner_user_id,
            _maybe_jsonb(payload),
            _maybe_jsonb(source_item_ids or []),
            action_detail_read_model.READ_MODEL_VERSION,
            source_updated_at,
        ),
    )
    _commit_if_supported(pg_conn)


def delete_action_detail_read_model_remote(
    action_id: str,
    *,
    viewer_scope: str | None = None,
    pg_conn: Any | None = None,
) -> None:
    if pg_conn is None:
        with connect() as conn:
            delete_action_detail_read_model_remote(action_id, viewer_scope=viewer_scope, pg_conn=conn)
            return
    params: dict[str, Any] = {"action_id": action_id}
    where = "action_id = %(action_id)s"
    if viewer_scope:
        where += " AND viewer_scope = %(viewer_scope)s"
        params["viewer_scope"] = viewer_scope
    pg_conn.execute(f"DELETE FROM {remote_schema()}.action_detail_read_models WHERE {where}", params)
    _commit_if_supported(pg_conn)


def build_action_detail_read_model_remote(
    action_id: str,
    *,
    request_user_id: str | None,
    can_view_all: bool,
    owner_user_id: str | None = None,
    execution_status: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any] | None:
    action = get_action_remote(action_id, user_id=None if can_view_all else owner_user_id)
    if not action:
        return None
    source_ids = action_detail_read_model.parse_source_item_ids(action.get("source_item_ids"))
    source_items = get_action_source_items_remote(
        source_ids,
        request_user_id=request_user_id,
        can_view_all=can_view_all,
    )
    payload = action_detail_read_model.build_action_detail_payload(
        action,
        source_items=source_items,
        execution_status=execution_status,
    )
    if persist:
        upsert_action_detail_read_model_remote(
            action_id=action_id,
            viewer_scope=action_detail_read_model.viewer_scope_for(can_view_all=can_view_all),
            owner_user_id=action.get("user_id"),
            payload=payload,
            source_item_ids=source_ids,
            source_updated_at=_action_source_updated_at(action),
        )
    return payload
