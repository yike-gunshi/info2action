from __future__ import annotations


def load_source_index_remote(pg_conn: Any | None = None) -> dict[str, Any] | None:
    """Load the remote sources registry index. Fail open for fetch/ingest paths."""
    try:
        if pg_conn is None:
            with connect() as conn:
                return load_source_index_remote(conn)
        import db

        rows = pg_conn.execute(
            f"SELECT id, platform, source_key, status, config_json FROM {remote_schema()}.sources"
        ).fetchall()
        return db.build_source_index_from_rows(rows)
    except Exception:
        return None


def list_active_sources_remote(
    platform: str,
    pg_conn: Any | None = None,
    *,
    fail_open: bool = True,
) -> list[dict[str, Any]]:
    """Return fetch-eligible remote sources, optionally propagating authority failures."""
    try:
        if pg_conn is None:
            with connect() as conn:
                return list_active_sources_remote(platform, conn, fail_open=fail_open)
        import db

        if platform == "x_user":
            rows = pg_conn.execute(
                f"""SELECT id, source_key, display_name, config_json
                    FROM {remote_schema()}.sources
                    WHERE platform=%s AND status IN ('active', 'broken', 'not_fetched')
                    ORDER BY id""",
                (platform,),
            ).fetchall()
        else:
            rows = pg_conn.execute(
                f"""SELECT id, source_key, display_name, config_json
                    FROM {remote_schema()}.sources
                    WHERE platform=%s AND status IN ('active', 'broken')
                    ORDER BY id""",
                (platform,),
            ).fetchall()
        return [db.normalize_active_source_row(row) for row in rows]
    except Exception:
        if fail_open:
            return []
        raise


def latest_x_user_watermark_remote(
    source_id: int | None,
    pg_conn: Any | None = None,
) -> str | None:
    """Return the newest persisted tweet id for one remote X source."""
    if source_id is None:
        return None
    if pg_conn is None:
        with connect() as conn:
            return latest_x_user_watermark_remote(source_id, pg_conn=conn)
    row = pg_conn.execute(
        f"""SELECT id
              FROM {remote_schema()}.items
             WHERE source_id = %s
               AND platform = 'twitter'
               AND published_at IS NOT NULL
             ORDER BY published_at DESC NULLS LAST
             LIMIT 1""",
        (source_id,),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def upsert_source_registry_remote(
    pg_conn: Any,
    *,
    platform: str,
    source_key: str,
    display_name: str,
    status: str,
    config_json: str | None,
    origin: str,
    now: str,
) -> str:
    """Upsert one remote registry source while preserving admin-owned fields."""
    schema = remote_schema()
    inserted = pg_conn.execute(
        f"""INSERT INTO {schema}.sources
              (platform, source_key, display_name, status, config_json, origin,
               created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform, source_key) DO NOTHING
            RETURNING id""",
        (
            platform,
            source_key,
            display_name,
            status,
            config_json,
            origin,
            now,
            now,
        ),
    ).fetchone()
    if inserted:
        return "inserted"

    pg_conn.execute(
        f"""UPDATE {schema}.sources
              SET display_name=%s, config_json=%s, updated_at=%s
            WHERE platform=%s AND source_key=%s""",
        (display_name, config_json, now, platform, source_key),
    )
    return "updated"


def record_source_fetch_result_remote(
    source_id: int | None,
    ok: bool,
    error: Any = None,
    broken_after: int = 5,
    pg_conn: Any | None = None,
) -> None:
    """Record one remote source fetch result without interrupting the fetch pipeline."""
    try:
        if source_id is None:
            return
        if pg_conn is None:
            with connect() as conn:
                record_source_fetch_result_remote(
                    source_id,
                    ok,
                    error=error,
                    broken_after=broken_after,
                    pg_conn=conn,
                )
                return

        schema = remote_schema()
        row = pg_conn.execute(
            f"SELECT status, consecutive_failures FROM {schema}.sources WHERE id = %s",
            (source_id,),
        ).fetchone()
        if row is None:
            return
        status = row["status"]
        if status not in {"active", "broken", "not_fetched"}:
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if ok:
            new_status = "active"
            pg_conn.execute(
                f"""UPDATE {schema}.sources
                      SET status = %s,
                          consecutive_failures = 0,
                          last_success_at = %s,
                          last_error = NULL,
                          updated_at = %s
                    WHERE id = %s""",
                (new_status, now, now, source_id),
            )
            commit = getattr(pg_conn, "commit", None)
            if commit:
                commit()
            return

        try:
            threshold = int(broken_after)
        except (TypeError, ValueError):
            threshold = 5
        if threshold <= 0:
            threshold = 5
        failures = int(row["consecutive_failures"] or 0) + 1
        new_status = "broken" if failures >= threshold else status
        last_error = None if error is None else str(error)[:500]
        pg_conn.execute(
            f"""UPDATE {schema}.sources
                  SET status = %s,
                      consecutive_failures = %s,
                      last_error = %s,
                      updated_at = %s
                WHERE id = %s""",
            (new_status, failures, last_error, now, source_id),
        )
        commit = getattr(pg_conn, "commit", None)
        if commit:
            commit()
    except Exception:
        return


def _source_identity_from_row(row: Any) -> str | None:
    raw_url = (_row_get(row, "url") or "").strip()
    item_id = _row_get(row, "id")
    if raw_url:
        try:
            from utils.url_normalize import normalize_url
            normalized = normalize_url(raw_url)
            if normalized.platform in ("twitter", "youtube") and normalized.canonical_url:
                return normalized.canonical_url
        except Exception:
            pass
        return raw_url
    return item_id


def _source_item_by_id(rows: list[Any]) -> dict[str, dict[str, Any]]:
    return {row["id"]: dict(row) for row in rows}


def lingowhale_group_counts_remote() -> dict[str, int]:
    cache_key = ("lingowhale_group_counts", remote_schema())
    cached = _cache_get_copy(cache_key)
    if cached is not None:
        return cached
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT COALESCE(detail_json ->> 'group', '') AS group_name,
                      COUNT(*) AS cnt
                 FROM {remote_schema()}.items
                WHERE platform = 'lingowhale'
                GROUP BY COALESCE(detail_json ->> 'group', '')"""
        ).fetchall()
    counts = {row["group_name"]: int(row["cnt"] or 0) for row in rows}
    return _cache_set_copy(cache_key, counts)


LINGOWHALE_GROUPS_SETTING_KEY = "lingowhale_groups"


def get_lingowhale_groups_metadata_remote() -> list[dict[str, Any]] | None:
    value = get_setting_remote(LINGOWHALE_GROUPS_SETTING_KEY)
    if isinstance(value, list):
        return value
    return None


def set_lingowhale_groups_metadata_remote(groups: list[dict[str, Any]]) -> None:
    set_setting_remote(LINGOWHALE_GROUPS_SETTING_KEY, groups)
    _cache_delete(("setting", LINGOWHALE_GROUPS_SETTING_KEY))


def _info_display_source_filter(alias: str) -> str:
    return f"({alias}.platform != 'twitter' OR COALESCE({alias}.source, '') != 'bookmarks')"


_CATEGORY_PRIORITY = {cid: idx for idx, cid in enumerate(ACTIVE_CATEGORY_IDS)}
_EVENT_SOURCE_PREVIEW_LIMIT = 3


def _build_event_source_metadata(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        cluster_id = int(row.get("cluster_id"))
        data = grouped.setdefault(
            cluster_id,
            {"source_preview": [], "_seen_sources": set(), "_category_counts": {}, "_media": []},
        )
        if row.get("platform") != "manual" and row.get("user_id") is None:
            data["_media"].extend(normalize_media(
                row.get("cover_url"), row.get("media_json"), source_url=row.get("url")
            ))
        category = _category_l1(row.get("ai_category"))
        if category:
            counts = data["_category_counts"]
            counts[category] = counts.get(category, 0) + 1

        platform = row.get("platform") or ""
        identity = (
            row.get("source_identity")
            or row.get("url")
            or f"{platform}:{row.get('author_name') or row.get('source') or row.get('item_id')}"
        )
        if identity in data["_seen_sources"]:
            continue
        data["_seen_sources"].add(identity)
        if len(data["source_preview"]) >= _EVENT_SOURCE_PREVIEW_LIMIT:
            continue
        data["source_preview"].append({
            "platform": platform,
            "author": row.get("author_name"),
            "source": row.get("source"),
        })

    result: dict[int, dict[str, Any]] = {}
    for cluster_id, data in grouped.items():
        category = None
        counts = data["_category_counts"]
        if counts:
            category = sorted(
                counts.items(),
                key=lambda item: (-item[1], _CATEGORY_PRIORITY.get(item[0], 999), item[0]),
            )[0][0]
        result[cluster_id] = {
            "category": category,
            "source_preview": data["source_preview"],
            "media_kind": media_kind(data["_media"]),
            "media": merge_media([data["_media"]]),
        }
    return result


def _fetch_event_source_metadata(conn: Any, schema: str, cluster_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not cluster_ids:
        return {}
    rows = conn.execute(
        f"""SELECT ci.cluster_id, ci.source_identity, ci.rank_in_cluster,
                  ci.is_primary_source,
                  i.id AS item_id, i.platform, i.author_name, i.source,
                  i.url, i.user_id, i.cover_url, i.media_json, i.ai_category, i.published_at, i.fetched_at
             FROM {schema}.cluster_items ci
             JOIN {schema}.items i ON i.id = ci.item_id
            WHERE ci.cluster_id = ANY(%(cluster_ids)s)
            ORDER BY ci.cluster_id ASC,
                     COALESCE(ci.is_primary_source, false) DESC,
                     ci.rank_in_cluster ASC NULLS LAST""",
        {"cluster_ids": cluster_ids},
    ).fetchall()
    normalized = [dict(r) for r in rows]
    normalized.sort(
        key=lambda r: (
            int(r.get("cluster_id") or 0),
            -int(bool(r.get("is_primary_source"))),
            int(r.get("rank_in_cluster") if r.get("rank_in_cluster") is not None else 999999),
            -sort_key(r.get("published_at") or r.get("fetched_at")),
        )
    )
    return _build_event_source_metadata(normalized)


def _info_group_source_value(group: str, source: str) -> str:
    return f"{group}{INFO_SCOPE_COMPOUND_SEPARATOR}{source}"
