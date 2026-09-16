from __future__ import annotations


def _key_tokens(key: tuple[Any, ...]):
    """key 中的可索引 token(含一层嵌套),供倒排索引/定向失效。"""
    for elem in key:
        if isinstance(elem, (tuple, list, set, frozenset)):
            for sub in elem:
                yield str(sub)
        else:
            yield str(elem)


def _auth_cache_ttl() -> int:
    # Keep revoked-session exposure bounded while still collapsing multi-endpoint
    # admin page loads behind the same access token.
    return min(_remote_cache_ttl(), _env_int(_runtime_env(), REMOTE_AUTH_CACHE_TTL_ENV, 60, min_value=0))


ADMIN_CONSOLE_TZ = ZoneInfo("Asia/Shanghai")


def _admin_console_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(ADMIN_CONSOLE_TZ).replace(microsecond=0)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ADMIN_CONSOLE_TZ).replace(microsecond=0)


def _admin_console_to_shanghai_iso(value: Any) -> str | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else parse_datetime(str(value))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ADMIN_CONSOLE_TZ).replace(microsecond=0).isoformat()


def _admin_console_age_hours(value: Any, now_utc: datetime) -> float | None:
    if not value:
        return None
    dt = value if isinstance(value, datetime) else parse_datetime(str(value))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 3600)


def _admin_console_age_text(value: Any, now_utc: datetime) -> str:
    age = _admin_console_age_hours(value, now_utc)
    if age is None:
        return "时间未知"
    if age < 1:
        return f"{max(0, int(age * 60))}m 前"
    return f"{int(age)}h 前"


def _admin_console_percent(value: float) -> str:
    percent = value * 100
    if abs(percent - round(percent)) < 0.05:
        return f"{int(round(percent))}%"
    return f"{percent:.1f}%"


def _admin_console_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _admin_console_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _admin_console_table_has_columns(
    conn: Any,
    schema: str,
    table_name: str,
    column_names: set[str],
) -> bool:
    rows = conn.execute(
        """SELECT column_name
             FROM information_schema.columns
            WHERE table_schema = %(schema_name)s
              AND table_name = %(table_name)s
              AND column_name = ANY(%(column_names)s)""",
        {
            "schema_name": schema,
            "table_name": table_name,
            "column_names": sorted(column_names),
        },
    ).fetchall()
    found = {str(row["column_name"]) for row in rows}
    return column_names.issubset(found)


def _admin_console_date_points(now_shanghai: datetime, days: int, value: Any) -> list[dict[str, Any]]:
    start = now_shanghai.date() - timedelta(days=days - 1)
    return [
        {"date": (start + timedelta(days=idx)).isoformat(), "value": value}
        for idx in range(days)
    ]


def _admin_console_trend(rows: list[Any], now_shanghai: datetime, days: int, default: Any) -> list[dict[str, Any]]:
    values = {str(row["date"]): row.get("value") for row in rows}
    points = _admin_console_date_points(now_shanghai, days, default)
    for point in points:
        if point["date"] in values:
            value = values[point["date"]]
            point["value"] = None if value is None else value
    return points


def _admin_console_disk_signal(used_percent: float | None, db_size: str | None = None) -> dict[str, Any]:
    signal = {
        "key": "disk",
        "level": "unknown",
        "label": "磁盘",
        "detail": "磁盘信息不可用",
        "link": None,
    }
    if used_percent is None:
        return signal
    if used_percent > 90:
        level = "crit"
    elif used_percent >= 80:
        level = "warn"
    else:
        level = "ok"
    db_text = db_size or "DB 未知"
    signal.update({
        "level": level,
        "detail": f"已用 {int(round(used_percent))}% · DB {db_text}",
    })
    return signal


def _admin_console_freshness_signal(rows: list[Any], now_utc: datetime) -> dict[str, Any]:
    signal = {
        "key": "freshness",
        "level": "unknown",
        "label": "平台新鲜度",
        "detail": "无平台抓取记录",
        "link": "runs",
    }
    platform_ages: list[tuple[str, float, Any]] = []
    for row in rows:
        platform = str(row.get("platform") or "unknown")
        last_fetched_at = row.get("last_fetched_at")
        age = _admin_console_age_hours(last_fetched_at, now_utc)
        if age is not None:
            platform_ages.append((platform, age, last_fetched_at))
    if not platform_ages:
        return signal

    worst = max(platform_ages, key=lambda item: item[1])
    level = classify_platform_freshness(worst[1])
    signal.update({
        "level": level,
        "detail": f"{worst[0]} 最近抓取 {int(worst[1])}h 前",
    })
    return signal


def _admin_console_remote_db_signal(started_at: float) -> dict[str, Any]:
    status()
    elapsed_ms = max(0, int(round((time.monotonic() - started_at) * 1000)))
    return {
        "key": "remote_db",
        "level": "ok",
        "label": "远程 DB",
        "detail": f"可达 · {elapsed_ms}ms",
        "link": None,
    }


def _admin_console_incidents(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incidents = [
        {
            "severity": signal["level"],
            "text": f"{signal['label']}：{signal['detail']}",
            "link": signal.get("link"),
        }
        for signal in signals
        if signal.get("level") in {"warn", "crit"}
    ]
    incidents.sort(key=lambda item: 0 if item["severity"] == "crit" else 1)
    return incidents[:5]


def _admin_console_db_size(conn: Any) -> str | None:
    row = conn.execute(
        "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size"
    ).fetchone()
    return (row or {}).get("db_size")


def _admin_console_disk_usage_percent() -> float | None:
    try:
        usage = shutil.disk_usage("/")
    except Exception:
        return None
    total = getattr(usage, "total", 0) or 0
    if total <= 0:
        return None
    return (float(getattr(usage, "used", 0) or 0) / float(total)) * 100


def admin_overview_remote(
    *,
    fetch_run_limit: int = 20,
    fetch_run_offset: int = 0,
    embedding_hours: float = 24,
    embedding_limit: int = 50,
    include_embedding: bool = False,
) -> dict[str, Any]:
    """Load initial admin dashboard data with one auth pass and one DB checkout."""
    limit = max(1, min(int(fetch_run_limit or 20), 100))
    offset = max(0, int(fetch_run_offset or 0))
    cache_key = (
        "admin_overview_result",
        remote_schema(),
        limit,
        offset,
        float(embedding_hours),
        max(1, min(int(embedding_limit or 50), 500)),
        bool(include_embedding),
    )
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached

    def _compute() -> dict[str, Any]:
        cached_inside = _cache_get_copy(cache_key)
        if cached_inside is not None:
            return cached_inside
        result = _admin_overview_remote_uncached(
            fetch_run_limit=limit,
            fetch_run_offset=offset,
            embedding_hours=embedding_hours,
            embedding_limit=embedding_limit,
            include_embedding=include_embedding,
        )
        return _cache_set_copy(cache_key, result)

    return _singleflight_sync(cache_key, _compute)


def _admin_overview_remote_uncached(
    *,
    fetch_run_limit: int = 20,
    fetch_run_offset: int = 0,
    embedding_hours: float = 24,
    embedding_limit: int = 50,
    include_embedding: bool = False,
) -> dict[str, Any]:
    """Load initial admin dashboard data with one auth pass and one DB checkout."""
    with connect() as conn:
        return {
            "codes": list_invite_codes_remote(pg_conn=conn),
            "users": list_users_remote(pg_conn=conn),
            "fetch_runs": {
                "runs": list_fetch_run_audits_remote(
                    limit=fetch_run_limit,
                    offset=fetch_run_offset,
                    pg_conn=conn,
                ),
                "limit": max(1, min(int(fetch_run_limit or 20), 100)),
                "offset": max(0, int(fetch_run_offset or 0)),
            },
            "embedding_usage": (
                get_embedding_usage_audit_remote(
                    hours=embedding_hours,
                    limit=embedding_limit,
                    pg_conn=conn,
                )
                if include_embedding
                else _empty_embedding_usage(embedding_hours, embedding_limit)
            ),
        }


def _normalize_interest_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    for field in ("keywords", "suggestion"):
        data[field] = _json_value(data.get(field))
        if field == "keywords" and data[field] is None:
            data[field] = []
    for col in ("created_at", "last_scan_at"):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def create_interest_remote(
    *,
    name: str,
    description: str | None = None,
    keywords: list[str] | None = None,
    sort: str = "relevance",
    item_limit: int = 30,
    scope: str = "all",
    user_id: str | None = None,
) -> int:
    with connect() as conn:
        row = conn.execute(
            f"""INSERT INTO {remote_schema()}.interests
                  (user_id, name, description, keywords, sort, item_limit, scope)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id""",
            (user_id, name, description, _maybe_jsonb(keywords or []), sort, item_limit, scope),
        ).fetchone()
        conn.commit()
        return _row_id(row)


def list_interests_remote(*, user_id: str | None = None) -> list[dict[str, Any]]:
    where = []
    params = {}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {remote_schema()}.interests {_where_sql(where)} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return [_normalize_interest_row(row) for row in rows]


def get_interest_remote(interest_id: int, *, user_id: str | None = None) -> dict[str, Any] | None:
    where = ["id = %(interest_id)s"]
    params = {"interest_id": interest_id}
    if user_id:
        where.append("user_id = %(user_id)s")
        params["user_id"] = user_id
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.interests {_where_sql(where)}",
            params,
        ).fetchone()
    return _normalize_interest_row(row) if row else None


def update_interest_remote(
    interest_id: int,
    *,
    owner_user_id: str | None = None,
    **fields: Any,
) -> bool:
    allowed = {
        "name", "description", "keywords", "sort", "item_limit", "scope",
        "enabled", "scan_status", "last_scan_at", "suggestion",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    sets = []
    params: dict[str, Any] = {"interest_id": interest_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        if key == "keywords":
            params[pname] = _maybe_jsonb(value)
        elif key == "suggestion" and isinstance(value, (dict, list)):
            params[pname] = json.dumps(value, ensure_ascii=False)
        else:
            params[pname] = value
    where = "id = %(interest_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE {remote_schema()}.interests SET {', '.join(sets)} WHERE {where}",
            params,
        )
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def delete_interest_remote(interest_id: int, *, owner_user_id: str | None = None) -> bool:
    if owner_user_id and not get_interest_remote(interest_id, user_id=owner_user_id):
        return False
    params = {"interest_id": interest_id}
    where = "id = %(interest_id)s"
    if owner_user_id:
        where += " AND user_id = %(owner_user_id)s"
        params["owner_user_id"] = owner_user_id
    with connect() as conn:
        conn.execute(
            f"DELETE FROM {remote_schema()}.interest_matches WHERE interest_id = %(interest_id)s",
            {"interest_id": interest_id},
        )
        cur = conn.execute(f"DELETE FROM {remote_schema()}.interests WHERE {where}", params)
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def get_interest_match_stats_remote(interest_id: int) -> dict[str, int]:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                      SUM(CASE WHEN is_new = 1 THEN 1 ELSE 0 END) AS new_count
                 FROM {remote_schema()}.interest_matches
                WHERE interest_id = %s""",
            (interest_id,),
        ).fetchone()
    return {"total": row["total"] or 0, "new_count": row["new_count"] or 0}


def get_interest_matches_remote(
    interest_id: int,
    sort: str = "relevance",
    limit: int = 30,
    offset: int = 0,
) -> list[dict[str, Any]]:
    order = "m.relevance_score DESC" if sort == "relevance" else "i.fetched_at DESC"
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT m.interest_id, m.item_id, m.relevance_score, m.is_new, m.matched_at,
                      i.platform, i.source, i.title, i.content, i.author_name, i.url,
                      i.cover_url, i.ai_summary, i.ai_key_points, i.ai_category,
                      i.relevance_score AS item_score, i.fetched_at, i.published_at,
                      NULL::timestamptz AS clicked_at, NULL::timestamptz AS starred_at
                 FROM {schema}.interest_matches m
                 JOIN {schema}.items i ON m.item_id = i.id
                WHERE m.interest_id = %s
                ORDER BY {order}
                LIMIT %s OFFSET %s""",
            (interest_id, limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_interest_matches_read_remote(interest_id: int) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.interest_matches SET is_new = 0 WHERE interest_id = %s",
            (interest_id,),
        )
        conn.commit()


def upsert_interest_matches_remote(interest_id: int, matches: list[dict[str, Any]]) -> None:
    with connect() as conn:
        for match in matches:
            conn.execute(
                f"""INSERT INTO {remote_schema()}.interest_matches
                      (interest_id, item_id, relevance_score, is_new, matched_at)
                    VALUES (%s, %s, %s, 1, now())
                    ON CONFLICT (interest_id, item_id) DO UPDATE SET
                      relevance_score = excluded.relevance_score,
                      matched_at = excluded.matched_at""",
                (interest_id, match["item_id"], match["relevance_score"]),
            )
        conn.commit()


def fetch_items_for_interest_scan_remote(
    *,
    scope: str = "all",
    since: Any = None,
) -> list[dict[str, Any]]:
    where = ["ai_summary IS NOT NULL", "ai_summary != ''"]
    params: dict[str, Any] = {}
    if since:
        where.append("fetched_at > %(since)s")
        params["since"] = since
    elif scope == "7d":
        where.append("fetched_at > now() - interval '7 days'")
    elif scope == "30d":
        where.append("fetched_at > now() - interval '30 days'")
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, ai_summary, ai_key_points
                  FROM {remote_schema()}.items
                 {_where_sql(where)}
                 ORDER BY fetched_at DESC""",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_interest_top_items_remote(interest_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.id, i.title, i.ai_summary
                  FROM {schema}.interest_matches m
                  JOIN {schema}.items i ON i.id = m.item_id
                 WHERE m.interest_id = %s
                 ORDER BY m.relevance_score DESC
                 LIMIT %s""",
            (interest_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _normalize_user_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    for col in (
        "created_at",
        "last_login_at",
        "verification_code_expires",
        "reset_token_expires",
    ):
        if col in data:
            data[col] = _timestamp_value(data.get(col))
    return data


def create_user_remote(user_id: str, username: str, email: str, password_hash: str, role: str = "user") -> dict[str, Any] | None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.users
                  (id, username, email, password_hash, role)
                VALUES (%s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return get_user_remote(user_id)


def create_user_with_invite_remote(
    user_id: str,
    username: str,
    email: str,
    password_hash: str,
    invite_code: str,
    verification_code: str,
    verification_code_expires: Any,
    role: str = "user",
) -> bool:
    """Create a user and consume an invite in one transaction."""
    schema = remote_schema()
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {schema}.users
                  (id, username, email, password_hash, role)
                VALUES (%s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role),
        )
        cur = conn.execute(
            f"""UPDATE {schema}.invite_codes
                   SET used_count = used_count + 1,
                       used_by = %s
                 WHERE code = %s
                   AND used_count < max_uses
                   AND (expires_at IS NULL OR expires_at > now())""",
            (user_id, invite_code),
        )
        if (getattr(cur, "rowcount", 0) or 0) <= 0:
            conn.rollback()
            return False
        conn.execute(
            f"""UPDATE {schema}.users
                   SET verification_code = %s,
                       verification_code_expires = %s
                 WHERE id = %s""",
            (verification_code, verification_code_expires, user_id),
        )
    clear_user_cache_keys(user_id)
    return True


def create_user_open_remote(
    user_id: str,
    username: str,
    email: str,
    password_hash: str,
    verification_code: str,
    verification_code_expires: Any,
    role: str = "user",
) -> bool:
    """P1-4 开放注册:创建用户并写入验证码,不消耗邀请码。"""
    schema = remote_schema()
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {schema}.users
                  (id, username, email, password_hash, role,
                   verification_code, verification_code_expires)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user_id, username, email, password_hash, role,
             verification_code, verification_code_expires),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return True


def get_user_remote(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE id = %s",
            (user_id,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_login_remote(login: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE email = %s OR username = %s",
            (login, login),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_username_remote(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE username = %s",
            (username,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_email_remote(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.users WHERE email = %s",
            (email,),
        ).fetchone()
    return _normalize_user_row(row)


def get_user_by_reset_token_remote(token: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, username, reset_token_expires
                  FROM {remote_schema()}.users
                 WHERE reset_token = %s""",
            (token,),
        ).fetchone()
    return _normalize_user_row(row)


def update_user_remote(user_id: str, **fields: Any) -> None:
    allowed = {
        "username", "email", "password_hash", "role", "discord_bot_token_enc",
        "last_login_at", "email_verified", "verification_code",
        "verification_code_expires", "reset_token", "reset_token_expires",
        "discord_channel_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = []
    params: dict[str, Any] = {"user_id": user_id}
    for idx, (key, value) in enumerate(updates.items()):
        pname = f"v{idx}"
        sets.append(f"{key} = %({pname})s")
        params[pname] = value
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.users SET {', '.join(sets)} WHERE id = %(user_id)s",
            params,
        )
        conn.commit()
    clear_user_cache_keys(user_id)


def list_users_remote(*, pg_conn: Any | None = None) -> list[dict[str, Any]]:
    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        rows = conn.execute(
            f"""SELECT id, username, email, role, created_at, last_login_at
                  FROM {remote_schema()}.users
                 ORDER BY created_at"""
        ).fetchall()
        return [_normalize_user_row(row) for row in rows]
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def get_invite_code_remote(code: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.invite_codes WHERE code = %s",
            (code,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["expires_at"] = _timestamp_value(data.get("expires_at"))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def use_invite_code_remote(code: str, user_id: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            f"""UPDATE {remote_schema()}.invite_codes
                   SET used_count = used_count + 1,
                       used_by = %s
                 WHERE code = %s
                   AND used_count < max_uses
                   AND (expires_at IS NULL OR expires_at > now())""",
            (user_id, code),
        )
        conn.commit()
        return (getattr(cur, "rowcount", 0) or 0) > 0


def create_invite_code_remote(code: str, created_by: str | None, max_uses: int = 1, expires_at: Any = None) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.invite_codes
                  (code, created_by, max_uses, expires_at)
                VALUES (%s, %s, %s, %s)""",
            (code, created_by, max_uses, expires_at),
        )
        conn.commit()


def list_invite_codes_remote(*, pg_conn: Any | None = None) -> list[dict[str, Any]]:
    conn_cm = None
    if pg_conn is None:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    else:
        conn = pg_conn
    try:
        rows = conn.execute(
            f"SELECT * FROM {remote_schema()}.invite_codes ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["expires_at"] = _timestamp_value(data.get("expires_at"))
            data["created_at"] = _timestamp_value(data.get("created_at"))
            out.append(data)
        return out
    finally:
        if conn_cm is not None:
            conn_cm.__exit__(None, None, None)


def delete_invite_code_remote(code: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.invite_codes WHERE code = %s", (code,))
        conn.commit()


def create_session_remote(session_id: str, user_id: str, token_type: str, expires_at: Any) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.sessions
                  (id, user_id, token_type, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  user_id = excluded.user_id,
                  token_type = excluded.token_type,
                  expires_at = excluded.expires_at""",
            (session_id, user_id, token_type, expires_at),
        )
        conn.commit()
    clear_user_cache_keys(user_id)


def create_sessions_remote(sessions: list[tuple[str, str, str, Any]]) -> None:
    if not sessions:
        return
    schema = remote_schema()
    sql = f"""INSERT INTO {schema}.sessions
                (id, user_id, token_type, expires_at)
              VALUES (%s, %s, %s, %s)
              ON CONFLICT (id) DO UPDATE SET
                user_id = excluded.user_id,
                token_type = excluded.token_type,
                expires_at = excluded.expires_at"""
    with connect() as conn:
        _executemany(conn, sql, sessions)
        conn.commit()
    # Each session tuple is (id, user_id, token_type, expires_at) — clear per-user.
    for sess in sessions:
        if len(sess) >= 2 and sess[1]:
            clear_user_cache_keys(sess[1])


def get_session_remote(session_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["expires_at"] = _timestamp_value(data.get("expires_at"))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def get_user_for_session_remote(session_id: str, user_id: str) -> dict[str, Any] | None:
    """Return a user only when the JWT session is still present and valid."""
    schema = remote_schema()
    cache_key = ("auth_session_user", schema, session_id, user_id)
    cached = _cache_get_with_ttl(cache_key, _auth_cache_ttl())
    if cached is not None:
        return cached
    with connect() as conn:
        row = conn.execute(
            f"""SELECT u.*, p.onboarding_completed AS profile_onboarding_completed
                  FROM {schema}.sessions s
                  JOIN {schema}.users u ON u.id = s.user_id
             LEFT JOIN {schema}.user_profiles p ON p.user_id = u.id
                 WHERE s.id = %s
                   AND s.user_id = %s
                   AND s.expires_at >= now()
                 LIMIT 1""",
            (session_id, user_id),
        ).fetchone()
    user = _normalize_user_row(row)
    if user and "profile_onboarding_completed" in user:
        user["_onboarding_completed"] = True if user["profile_onboarding_completed"] is None else bool(user["profile_onboarding_completed"])
    return _cache_set_with_ttl(cache_key, user, _auth_cache_ttl())


def finish_login_remote(
    user_id: str,
    *,
    access_jti: str,
    access_expires_at: Any,
    refresh_jti: str,
    refresh_expires_at: Any,
    last_login_at: Any,
) -> dict[str, Any] | None:
    """Persist login side effects in one remote round-trip and return profile."""
    schema = remote_schema()
    with connect() as conn:
        profile = conn.execute(
            f"""
            WITH upd_user AS (
                UPDATE {schema}.users
                   SET last_login_at = %(last_login_at)s
                 WHERE id = %(user_id)s
             RETURNING id
            ),
            upsert_sessions AS (
                INSERT INTO {schema}.sessions (id, user_id, token_type, expires_at)
                VALUES
                    (%(access_jti)s,  %(user_id)s, 'access',  %(access_expires_at)s),
                    (%(refresh_jti)s, %(user_id)s, 'refresh', %(refresh_expires_at)s)
                ON CONFLICT (id) DO UPDATE SET
                    user_id = excluded.user_id,
                    token_type = excluded.token_type,
                    expires_at = excluded.expires_at
                RETURNING id
            )
            SELECT p.*
              FROM {schema}.user_profiles p
             WHERE p.user_id = %(user_id)s
            """,
            {
                "user_id": user_id,
                "last_login_at": last_login_at,
                "access_jti": access_jti,
                "access_expires_at": access_expires_at,
                "refresh_jti": refresh_jti,
                "refresh_expires_at": refresh_expires_at,
            },
        ).fetchone()
        conn.commit()
    clear_user_cache_keys(user_id)
    if not profile:
        return None
    data = dict(profile)
    for field in ("interests", "tools", "manifest"):
        data[field] = _json_value(data.get(field))
    return data


def refresh_access_session_remote(
    *,
    refresh_jti: str,
    user_id: str,
    access_jti: str,
    access_expires_at: Any,
) -> dict[str, Any] | None:
    """Validate refresh session and insert a new access session in one trip."""
    schema = remote_schema()
    with connect() as conn:
        user = conn.execute(
            f"""SELECT u.*
                  FROM {schema}.sessions s
                  JOIN {schema}.users u ON u.id = s.user_id
                 WHERE s.id = %s
                   AND s.user_id = %s
                   AND s.token_type = 'refresh'
                   AND s.expires_at >= now()
                 LIMIT 1""",
            (refresh_jti, user_id),
        ).fetchone()
        if not user:
            return None
        conn.execute(
            f"""INSERT INTO {schema}.sessions
                  (id, user_id, token_type, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                  user_id = excluded.user_id,
                  token_type = excluded.token_type,
                  expires_at = excluded.expires_at""",
            (access_jti, user_id, "access", access_expires_at),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return _normalize_user_row(user)


def delete_user_sessions_remote(user_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE user_id = %s", (user_id,))
        conn.commit()
    clear_user_cache_keys(user_id)


def delete_session_remote(session_id: str) -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE id = %s", (session_id,))
        conn.commit()
    # Don't know user_id from session_id alone. Auth cache TTL=10s catches stale.


def cleanup_expired_sessions_remote() -> None:
    with connect() as conn:
        conn.execute(f"DELETE FROM {remote_schema()}.sessions WHERE expires_at < now()")
        conn.commit()
    # Bulk cleanup; user_ids unknown. Auth cache TTL=10s catches stale.


def get_user_profile_remote(user_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM {remote_schema()}.user_profiles WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    for field in ("interests", "tools", "manifest"):
        data[field] = _json_value(data.get(field))
    return data


_PROFILE_SENTINEL = object()


def upsert_user_profile_remote(
    user_id: str,
    *,
    role: Any = _PROFILE_SENTINEL,
    interests: Any = _PROFILE_SENTINEL,
    tools: Any = _PROFILE_SENTINEL,
    manifest: Any = _PROFILE_SENTINEL,
    onboarding_completed: Any = _PROFILE_SENTINEL,
) -> dict[str, Any] | None:
    fields: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if role is not _PROFILE_SENTINEL:
        fields["role"] = role
    if interests is not _PROFILE_SENTINEL:
        fields["interests"] = _maybe_jsonb(interests)
    if tools is not _PROFILE_SENTINEL:
        fields["tools"] = _maybe_jsonb(tools)
    if manifest is not _PROFILE_SENTINEL:
        fields["manifest"] = _maybe_jsonb(manifest)
    if onboarding_completed is not _PROFILE_SENTINEL:
        fields["onboarding_completed"] = 1 if onboarding_completed else 0

    columns = ["user_id", *fields.keys()]
    values = [user_id, *fields.values()]
    placeholders = ", ".join(["%s"] * len(values))
    updates = ", ".join(f"{col} = excluded.{col}" for col in fields)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.user_profiles ({', '.join(columns)})
                VALUES ({placeholders})
                ON CONFLICT (user_id) DO UPDATE SET {updates}""",
            tuple(values),
        )
        conn.commit()
    clear_user_cache_keys(user_id)
    return get_user_profile_remote(user_id)


def _normalize_briefing_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    data = dict(row)
    for col in ("insights", "suggestions"):
        data[col] = _json_value(data.get(col))
    data["created_at"] = _timestamp_value(data.get("created_at"))
    return data


def upsert_briefing_remote(
    briefing_id: str,
    date: str,
    insights: list[dict[str, Any]] | None,
    suggestions: list[dict[str, Any]] | None,
    input_count: int,
    model: str,
) -> None:
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.briefings
                  (id, date, insights, suggestions, input_count, model, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                  date = excluded.date,
                  insights = excluded.insights,
                  suggestions = excluded.suggestions,
                  input_count = excluded.input_count,
                  model = excluded.model,
                  created_at = excluded.created_at""",
            (
                briefing_id,
                date,
                json.dumps(insights or [], ensure_ascii=False),
                json.dumps(suggestions or [], ensure_ascii=False),
                input_count,
                model,
            ),
        )
        conn.commit()


def get_briefing_remote(date: str | None = None) -> dict[str, Any] | None:
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            f"""SELECT *
                  FROM {remote_schema()}.briefings
                 WHERE date = %s
                 ORDER BY created_at DESC
                 LIMIT 1""",
            (date,),
        ).fetchone()
    return _normalize_briefing_row(row)


def list_briefing_dates_remote(limit: int = 30) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT date
                  FROM {remote_schema()}.briefings
                 ORDER BY date DESC
                 LIMIT %s""",
            (limit,),
        ).fetchall()
    return [row["date"] for row in rows]


def get_setting_remote(key: str) -> Any:
    cache_key = ("setting", key)
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    with connect() as conn:
        row = conn.execute(
            f"SELECT value FROM {remote_schema()}.settings WHERE key = %s",
            (key,),
        ).fetchone()
    value = _json_value(row["value"]) if row else None
    return _cache_set_copy(cache_key, value)


def set_setting_remote(key: str, value: Any) -> None:
    value_text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    with connect() as conn:
        conn.execute(
            f"""INSERT INTO {remote_schema()}.settings (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at""",
            (key, value_text),
        )
        conn.commit()
    _cache_delete(("setting", key))


def get_generation_usage_today_remote(
    pg_conn: Any | None = None,
    *,
    user_id: str,
    limit: int,
) -> dict[str, Any]:
    """Return today's generation quota snapshot from Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return get_generation_usage_today_remote(conn, user_id=user_id, limit=limit)
    today = _asr_today_cst()
    row = pg_conn.execute(
        f"""SELECT count
              FROM {remote_schema()}.user_daily_generation
             WHERE user_id = %s AND day_cst = %s""",
        (str(user_id), today),
    ).fetchone()
    used = int(_row_get(row, "count", 0) or 0) if row else 0
    return _generation_usage_snapshot(today, used, limit)


def _apply_user_status_overlay(
    *,
    schema: str,
    items: list[dict[str, Any]],
    user_id: str | None,
) -> list[dict[str, Any]]:
    if not user_id or not items:
        return items
    item_ids = [str(item.get("id") or "").strip() for item in items if str(item.get("id") or "").strip()]
    if not item_ids:
        return items
    try:
        with connect() as conn:
            rows = conn.execute(
                f"""SELECT item_id, read_at, clicked_at, starred_at, hidden_at
                      FROM {schema}.item_status
                     WHERE user_id = %(status_user_id)s
                       AND item_id = ANY(%(item_ids)s)""",
                {"status_user_id": user_id, "item_ids": item_ids},
            ).fetchall()
    except Exception:
        return items
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        item_id = str(data.get("item_id") or "")
        if item_id:
            by_id[item_id] = data
    if not by_id:
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        status = by_id.get(str(item.get("id") or ""))
        if not status:
            out.append(item)
            continue
        next_item = dict(item)
        for key in ("read_at", "clicked_at", "starred_at", "hidden_at"):
            if key in status:
                next_item[key] = _timestamp_value(status.get(key))
        out.append(next_item)
    return out


def _apply_user_status_overlay_to_sections(
    *,
    schema: str,
    sections: dict[str, list[dict[str, Any]]],
    user_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    if not user_id or not sections:
        return sections
    flat_items: list[dict[str, Any]] = []
    positions: list[tuple[str, int]] = []
    for section_key, items in sections.items():
        for index, item in enumerate(items):
            flat_items.append(item)
            positions.append((section_key, index))
    overlaid = _apply_user_status_overlay(schema=schema, items=flat_items, user_id=user_id)
    next_sections = {key: list(items) for key, items in sections.items()}
    for (section_key, index), item in zip(positions, overlaid, strict=False):
        next_sections[section_key][index] = item
    return next_sections
