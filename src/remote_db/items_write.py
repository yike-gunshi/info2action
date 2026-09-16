from __future__ import annotations


def _item_write_value(column: str, item: dict[str, Any]) -> Any:
    value = item.get(column)
    if column in REMOTE_ITEM_JSONB_COLUMNS:
        return _maybe_jsonb(value)
    if column in REMOTE_ITEM_TIMESTAMP_COLUMNS:
        return _timestamp_value(value)
    if column == "visible" and value is None:
        return 1
    return value


REMOTE_ITEM_LIGHT_UPDATE_COLUMNS = {
    "title",
    "content",
    "author_name",
    "cover_url",
    "media_json",
    "metrics_json",
    "detail_json",
    "comments_json",
    "published_at",
}


def update_item_light_fields_remote(
    pg_conn: Any | None,
    item_id: str,
    updates: dict[str, Any],
) -> None:
    """Update mutable item fields without attempting an insert.

    ``upsert_item_remote`` must provide required insert columns such as platform
    and source. Submit-url refreshes for existing items only need to enrich a few
    display fields, so an UPDATE avoids tripping insert-time NOT NULL checks.
    """
    if pg_conn is None:
        with connect() as conn:
            update_item_light_fields_remote(conn, item_id, updates)
            return

    clean_updates: dict[str, Any] = {}
    for column in REMOTE_ITEM_LIGHT_UPDATE_COLUMNS:
        if column not in updates:
            continue
        value = updates.get(column)
        if value is None:
            continue
        clean_updates[column] = _item_write_value(column, updates)

    if not clean_updates:
        return

    columns = sorted(clean_updates)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    values = [clean_updates[column] for column in columns]
    pg_conn.execute(
        f"UPDATE {remote_schema()}.items SET {assignments} WHERE id = %s",
        values + [item_id],
    )


REMOTE_ID_SEQUENCES = {
    "fetch_runs": "fetch_runs_id_seq",
    "clusters": "clusters_id_seq",
    "cluster_judge_log": "cluster_judge_log_id_seq",
}


def _ensure_remote_id_sequence(pg_conn: Any, table: str) -> None:
    """Keep Postgres id sequences ahead of rows imported with SQLite ids."""
    sequence = REMOTE_ID_SEQUENCES[table]
    schema = remote_schema()
    pg_conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
        (f"{schema}.{table}.id_sequence",),
    )
    pg_conn.execute(
        f"""SELECT setval(
              '{schema}.{sequence}'::regclass,
              greatest(coalesce((select max(id) from {schema}.{table}), 0), 1),
              true
            )"""
    )


def upsert_item_remote(
    pg_conn: Any,
    item_dict: dict[str, Any],
    *,
    fetch_run_id: int | None = None,
) -> None:
    """Insert/update an item in Supabase and track fetch_run_items."""
    if pg_conn is None:
        with connect() as conn:
            upsert_item_remote(conn, item_dict, fetch_run_id=fetch_run_id)
            return

    item = dict(item_dict)
    if fetch_run_id is not None:
        item["fetch_run_id"] = fetch_run_id

    schema = remote_schema()
    item_id = item.get("id")
    run_id = item.get("fetch_run_id")
    run_exists = False
    existed_before = None
    if run_id is not None:
        run_exists = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.fetch_runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            is not None
        )
        existed_before = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.items WHERE id = %s",
                (item_id,),
            ).fetchone()
            is not None
        )

    columns = REMOTE_ITEM_WRITE_COLUMNS
    values = [_item_write_value(col, item) for col in columns]
    pg_conn.execute(_item_upsert_sql(schema), values)

    if run_id is not None and run_exists:
        was_inserted = 0 if existed_before else 1
        pg_conn.execute(
            _fetch_run_item_upsert_sql(schema),
            (run_id, item_id, item.get("platform"), item.get("source"), was_inserted),
        )


def batch_upsert_items_remote(
    pg_conn: Any | None,
    items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    fetch_run_id: int | None = None,
) -> int:
    """Upsert a batch of items into Supabase in one transaction."""
    if pg_conn is None:
        with connect() as conn:
            return batch_upsert_items_remote(conn, items, fetch_run_id=fetch_run_id)

    batch = [dict(item) for item in items]
    if not batch:
        return 0
    if fetch_run_id is not None:
        for item in batch:
            item["fetch_run_id"] = fetch_run_id
    has_duplicate_ids = _has_duplicate_item_ids(batch)

    schema = remote_schema()
    run_exists = False
    existing_ids: set[str] = set()
    if fetch_run_id is not None:
        run_exists = (
            pg_conn.execute(
                f"SELECT 1 AS exists FROM {schema}.fetch_runs WHERE id = %s",
                (fetch_run_id,),
            ).fetchone()
            is not None
        )
        ids = [str(item.get("id")) for item in batch if item.get("id")]
        if ids:
            rows = pg_conn.execute(
                f"SELECT id FROM {schema}.items WHERE id = ANY(%s)",
                (ids,),
            ).fetchall()
            existing_ids = {str(row["id"] if isinstance(row, dict) else row[0]) for row in rows}

    values = [[_item_write_value(col, item) for col in REMOTE_ITEM_WRITE_COLUMNS] for item in batch]
    if has_duplicate_ids:
        _executemany(pg_conn, _item_upsert_sql(schema), values)
    else:
        _execute_multirow_upsert(
            pg_conn,
            lambda row_count: _item_upsert_sql(schema, row_count=row_count),
            values,
        )

    if fetch_run_id is not None and run_exists:
        run_rows = [
            (
                fetch_run_id,
                item.get("id"),
                item.get("platform"),
                item.get("source"),
                0 if str(item.get("id")) in existing_ids else 1,
            )
            for item in batch
            if item.get("id")
        ]
        if run_rows:
            if has_duplicate_ids:
                _executemany(pg_conn, _fetch_run_item_upsert_sql(schema), run_rows)
            else:
                _execute_multirow_upsert(
                    pg_conn,
                    lambda row_count: _fetch_run_item_upsert_sql(schema, row_count=row_count),
                    run_rows,
                )
    _commit_if_supported(pg_conn)
    return len(batch)


def query_pending_enrichment_items_remote(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    run_id: int | None = None,
    run_items_scope: str = "tagged",
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
) -> list[dict[str, Any]]:
    """Return items needing enrichment from Supabase."""
    schema = remote_schema()
    item_alias = "items"
    from_clause = f"{schema}.items"
    select_cols = f"""{item_alias}.id, {item_alias}.platform, {item_alias}.source,
                     {item_alias}.author_name, {item_alias}.metrics_json,
                     {item_alias}.url, {item_alias}.title, {item_alias}.content,
                     {item_alias}.ai_summary, {item_alias}.ai_category as category,
                     {item_alias}.detail_json, {item_alias}.asr_text"""
    params: list[Any] = []
    clauses = [f"{item_alias}.platform <> 'bilibili'"]

    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        clauses = [f"{item_alias}.id IN ({placeholders})"]
        params.extend(ids)
    else:
        if run_id is not None:
            if run_items_scope == "inserted":
                item_alias = "i"
                from_clause = (
                    f"{schema}.fetch_run_items fri "
                    f"JOIN {schema}.items {item_alias} ON {item_alias}.id = fri.item_id"
                )
                select_cols = f"""{item_alias}.id, {item_alias}.platform, {item_alias}.source,
                                 {item_alias}.author_name, {item_alias}.metrics_json,
                                 {item_alias}.url, {item_alias}.title, {item_alias}.content,
                                 {item_alias}.ai_summary, {item_alias}.ai_category as category,
                                 {item_alias}.detail_json, {item_alias}.asr_text"""
                clauses = [
                    f"{item_alias}.platform <> 'bilibili'",
                    "fri.run_id = %s",
                    "fri.was_inserted = 1",
                ]
                params.append(run_id)
            elif run_items_scope == "tagged":
                clauses.append(f"{item_alias}.fetch_run_id = %s")
                params.append(run_id)
            else:
                raise RemoteDBConfigError(f"Unsupported run_items_scope={run_items_scope!r}")
        time_expr = (
            f"{item_alias}.published_at"
            if require_published_at
            else f"COALESCE({item_alias}.published_at, {item_alias}.fetched_at)"
        )
        if require_published_at:
            clauses.append(f"{item_alias}.published_at IS NOT NULL")
        if window_start:
            clauses.append(f"{time_expr} >= %s")
            params.append(_timestamp_value(window_start))
        if window_end:
            clauses.append(f"{time_expr} < %s")
            params.append(_timestamp_value(window_end))
        clauses.extend(
            [
                f"({item_alias}.ai_retry_after IS NULL OR {item_alias}.ai_retry_after <= now())",
                f"""(
                    {item_alias}.ai_summary IS NULL OR {item_alias}.ai_summary = ''
                    OR {item_alias}.ai_quality_score IS NULL
                    OR {item_alias}.ai_category IS NULL OR {item_alias}.ai_category = ''
                    OR {item_alias}.ai_categories IS NULL
                )""",
            ]
        )

    order_expr = (
        f"COALESCE({item_alias}.published_at, {item_alias}.fetched_at)"
        if (window_start or window_end)
        else f"{item_alias}.fetched_at"
    )
    limit_clause = ""
    if limit:
        limit_clause = " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        set_pending_scan_statement_timeout(conn)
        rows = conn.execute(
            f"""SELECT {select_cols}
                  FROM {from_clause}
                 WHERE {' AND '.join(clauses)}
                 ORDER BY {order_expr} DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def write_enrichment_remote(pg_conn: Any | None, item_id: str, parsed: dict[str, Any]) -> None:
    """Write enrichment output to a Supabase item row."""
    if pg_conn is None:
        with connect() as conn:
            write_enrichment_remote(conn, item_id, parsed)
            return

    key_points = parsed.get("key_points")
    keywords = parsed.get("keywords")
    dimensions = parsed.get("dimensions")
    categories = parsed.get("categories")
    subcategories = parsed.get("subcategories")
    ai_extracted = parsed.get("ai_extracted") or {}
    has_extracted = bool(
        ai_extracted.get("skills")
        or ai_extracted.get("models")
        or ai_extracted.get("event_card")
    )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET ai_summary = %s,
                   ai_key_points = %s,
                   ai_category = COALESCE(%s, ai_category),
                   content_type = %s,
                   ai_dimensions = %s,
                   ai_quality_score = %s,
                   relevance_score = COALESCE(%s, relevance_score),
                   ai_keywords = %s,
                   ai_categories = %s,
                   ai_subcategories = %s,
                   multi_l1_reason = %s,
                   ai_extracted = %s,
                   visible = %s,
                   ai_error_count = 0,
                   ai_last_error = NULL,
                   ai_last_error_at = NULL,
                   ai_retry_after = NULL
             WHERE id = %s""",
        (
            parsed.get("summary"),
            json.dumps(key_points, ensure_ascii=False) if key_points else None,
            parsed.get("category"),
            parsed.get("content_type"),
            _maybe_jsonb(dimensions) if dimensions else None,
            parsed.get("quality_score"),
            parsed.get("relevance_score"),
            json.dumps(keywords, ensure_ascii=False) if keywords else None,
            _maybe_jsonb(categories) if categories else None,
            _maybe_jsonb(subcategories) if subcategories else None,
            parsed.get("multi_l1_reason"),
            _maybe_jsonb(ai_extracted) if has_extracted else None,
            1 if parsed.get("visible", True) else 0,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def record_ai_failure_remote(
    pg_conn: Any | None,
    item_id: str,
    error: str,
    *,
    retry_after: Any = None,
    increment: bool = True,
) -> None:
    """Record item-level AI failure metadata in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            record_ai_failure_remote(
                conn,
                item_id,
                error,
                retry_after=retry_after,
                increment=increment,
            )
            return

    retry_after_value = None
    if isinstance(retry_after, (int, float)):
        retry_after_value = datetime.now(timezone.utc).timestamp() + float(retry_after)
        retry_after_value = datetime.fromtimestamp(retry_after_value, tz=timezone.utc)
    elif retry_after:
        retry_after_value = _timestamp_value(retry_after)
    count_expr = "COALESCE(ai_error_count, 0) + 1" if increment else "COALESCE(ai_error_count, 0)"
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET ai_error_count = {count_expr},
                   ai_last_error = %s,
                   ai_last_error_at = %s,
                   ai_retry_after = %s
             WHERE id = %s""",
        (str(error)[:500], datetime.now(timezone.utc), retry_after_value, item_id),
    )
    _commit_if_supported(pg_conn)


def write_judge_log_remote(
    pg_conn: Any | None,
    *,
    item_id: str,
    candidate_cluster_ids: list[int],
    estimated_input_tokens: int | None,
    matches: list[dict] | None,
    selected_cluster_id: int | None,
    selection_reason: str,
    possible_merge_candidates: list[int],
    decision_model: str,
) -> int | None:
    """Insert a cluster judge decision row in Supabase."""
    if pg_conn is None:
        with connect() as conn:
            return write_judge_log_remote(
                conn,
                item_id=item_id,
                candidate_cluster_ids=candidate_cluster_ids,
                estimated_input_tokens=estimated_input_tokens,
                matches=matches,
                selected_cluster_id=selected_cluster_id,
                selection_reason=selection_reason,
                possible_merge_candidates=possible_merge_candidates,
                decision_model=decision_model,
            )
    _ensure_remote_id_sequence(pg_conn, "cluster_judge_log")
    row = pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.cluster_judge_log
              (item_id, candidate_cluster_ids, llm_input_tokens,
               llm_output_tokens, matches_json, selected_cluster_id,
               selection_reason, possible_merge_candidates, decision_model,
               created_at)
            VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, now())
            RETURNING id""",
        (
            item_id,
            _maybe_jsonb(candidate_cluster_ids),
            estimated_input_tokens,
            _maybe_jsonb(matches) if matches is not None else None,
            selected_cluster_id,
            selection_reason,
            _maybe_jsonb(possible_merge_candidates),
            decision_model,
        ),
    ).fetchone()
    _commit_if_supported(pg_conn)
    return _row_id(row) if row is not None else None


def record_keywords_remote(keywords: list[str], platform: str) -> None:
    if not keywords:
        return
    now = datetime.now(timezone.utc)
    with connect() as conn:
        for keyword in keywords:
            kw = str(keyword or "").strip()
            if not kw:
                continue
            conn.execute(
                f"""INSERT INTO {remote_schema()}.search_keywords
                      (keyword, platform, last_used_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (keyword, platform) DO UPDATE SET
                      last_used_at = excluded.last_used_at""",
                (kw, platform, now),
            )
        conn.commit()


def query_link_enrichment_items_remote(limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id, title, content, detail_json, url
                  FROM {remote_schema()}.items
                 WHERE fetched_at > now() - interval '2 days'
                   AND (detail_json IS NULL OR NOT (detail_json ? 'referenced_urls'))
                 ORDER BY fetched_at DESC
                 LIMIT %s""",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["detail_json"] = _json_value(item.get("detail_json"))
        out.append(item)
    return out


def update_item_detail_json_remote(item_id: str, detail_json: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {remote_schema()}.items SET detail_json = %s WHERE id = %s",
            (_maybe_jsonb(detail_json), item_id),
        )
        conn.commit()


def get_all_interest_keywords_remote() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT keywords FROM {remote_schema()}.interests WHERE enabled = 1"
        ).fetchall()
    out: list[str] = []
    seen = set()
    for row in rows:
        keywords = _json_value(row.get("keywords")) or []
        if not isinstance(keywords, list):
            continue
        for kw in keywords:
            text = str(kw or "").strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
    return out


def get_submit_existing_item_remote(item_id: str, url: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, title, content, ai_summary, url
                  FROM {remote_schema()}.items
                 WHERE id = %s OR url = %s
                 ORDER BY CASE WHEN id = %s THEN 0 ELSE 1 END
                 LIMIT 1""",
            (item_id, url, item_id),
        ).fetchone()
    return dict(row) if row else None


def get_item_media_json_remote(item_id: str) -> Any:
    """Return `items.media_json` from Supabase for media/ASR helpers."""
    with connect() as conn:
        row = conn.execute(
            f"SELECT media_json FROM {remote_schema()}.items WHERE id = %s",
            (item_id,),
        ).fetchone()
    return _json_value(_row_get(row, "media_json")) if row else None


def get_media_item_remote(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            f"""SELECT id, user_id, platform, media_json
                  FROM {remote_schema()}.items
                 WHERE id = %s""",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["media_json"] = _json_value(data.get("media_json"))
    return data


def get_twitter_mp4_url_remote(item_id: str) -> str | None:
    item = get_media_item_remote(item_id)
    if not item or item.get("platform") != "twitter":
        return None
    media = item.get("media_json") or []
    if isinstance(media, str):
        media = _json_value(media)
    if not isinstance(media, list):
        return None
    for entry in media:
        if isinstance(entry, dict) and entry.get("type") == "video" and entry.get("url"):
            return entry["url"]
    return None


def _media_urls_from_item(cover_url: Any, media_json: Any) -> list[str]:
    return image_urls(normalize_media(cover_url, _json_value(media_json)))
