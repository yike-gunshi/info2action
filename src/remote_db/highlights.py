from __future__ import annotations


def compute_highlight_score(
    *,
    max_q: float | None,
    avg_q: float | None,
    scored_include_count: int,
    unique_source_count: int,
) -> float | None:
    if not scored_include_count or max_q is None or avg_q is None:
        return None
    quality = HIGHLIGHT_SCORE_W_MAX * float(max_q) + HIGHLIGHT_SCORE_W_AVG * float(avg_q)
    n = float(scored_include_count)
    shrunk = (n * quality + HIGHLIGHT_SCORE_SHRINK_K * HIGHLIGHT_SCORE_PRIOR) / (
        n + HIGHLIGHT_SCORE_SHRINK_K
    )
    evidence = math.log1p(max(int(unique_source_count or 0), 1))
    norm = math.log1p(HIGHLIGHT_SCORE_EVIDENCE_NORM_SOURCES)
    return round(100.0 * shrunk * evidence / norm, 2)


def _highlights_read_model_enabled(env: dict[str, str] | None = None) -> bool:
    return _truthy((env or _runtime_env()).get(HIGHLIGHTS_READ_MODEL_ENV))


def _highlights_stale_fallback_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_STALE_FALLBACK_ENV, default=True)


def _highlights_request_freshness_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_REQUEST_FRESHNESS_ENV, default=False)


def _highlights_self_heal_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_SELF_HEAL_ENV, default=True)


def _highlights_refresh_skip_during_fetch_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_REFRESH_SKIP_DURING_FETCH_ENV, default=True)


def _highlights_read_model_incremental_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_READ_MODEL_INCREMENTAL_ENV, default=True)


def _highlights_verdict_filter_enabled(env: dict[str, str] | None = None) -> bool:
    return _env_bool(env or _runtime_env(), HIGHLIGHTS_VERDICT_FILTER_ENV, default=False)


def _highlights_verdict_filter_recent_days(env: dict[str, str] | None = None) -> int:
    return _env_int(env or _runtime_env(), HIGHLIGHTS_VERDICT_FILTER_RECENT_DAYS_ENV, 0, min_value=0)


def _highlights_display_threshold(env: dict[str, str] | None = None) -> float | None:
    values = _runtime_env() if env is None else env
    raw = (values.get(HIGHLIGHTS_DISPLAY_THRESHOLD_ENV) or "").strip()
    if not raw:
        return None
    try:
        threshold = float(raw)
    except (TypeError, ValueError):
        threshold = math.nan
    if not math.isfinite(threshold):
        logging.getLogger(__name__).warning(
            "%s=%r is invalid; highlights display gate disabled",
            HIGHLIGHTS_DISPLAY_THRESHOLD_ENV,
            raw,
        )
        return None
    return threshold


def _highlights_display_cluster_condition(
    schema: str,
    cluster_alias: str,
    *,
    threshold: float | None,
) -> str:
    """Return the single production/admin predicate for cluster display."""
    manual_hide = f"""EXISTS (
        SELECT 1
          FROM {schema}.highlight_cluster_decisions hcd
         WHERE hcd.cluster_id = {cluster_alias}.id
           AND hcd.manual_display = 'force_hide'
      )"""
    if threshold is None:
        return f"(NOT {manual_hide})"
    threshold_sql = repr(float(threshold))
    return f"""(
      NOT {manual_hide}
      AND {cluster_alias}.why_read IS NOT NULL
      AND EXISTS (
        SELECT 1
          FROM {schema}.highlight_cluster_decisions hcd
         WHERE hcd.cluster_id = {cluster_alias}.id
           AND (
             hcd.manual_display = 'force_show'
             OR (hcd.score_inputs->>'max_flag_score10')::float >= {threshold_sql}
           )
      )
    )"""


def _highlights_display_cluster_filter(
    schema: str,
    cluster_alias: str,
    *,
    threshold: float | None,
) -> str:
    condition = _highlights_display_cluster_condition(
        schema,
        cluster_alias,
        threshold=threshold,
    )
    return f"\n      AND {condition}\n    "


def _highlights_summary_cluster_filter(schema: str, cluster_alias: str) -> str:
    """Return the production summary-gate predicate shared with admin audit views."""
    return f"""
      EXISTS (
        SELECT 1
          FROM {schema}.highlight_cluster_decisions d
         WHERE d.cluster_id = {cluster_alias}.id
           AND d.decision = 'included'
           AND d.cluster_verdict IN ('featured', 'positive_borderline')
      )
    """.strip()


def _highlights_verdict_cluster_filter(
    schema: str,
    cluster_alias: str,
    env: dict[str, str] | None = None,
) -> str:
    if not _highlights_verdict_filter_enabled(env):
        return ""
    recent_days = _highlights_verdict_filter_recent_days(env)
    # perf-v27 P1: 改读簇级预计算表 highlight_cluster_decisions(PK join)。
    # 旧形态对窗口内每簇现场做 cluster_items JOIN items 的 EXISTS,在生产实测
    # 把物化拖到 24-93s(BF-0711-1 饱和事故根因);新形态生产实测 0.02s。
    # decisions 行由 _sync_highlight_cluster_decisions 在每次刷新内先行增量维护
    # (全量/delta 两路径均已保证 sync 先于 scope 物化),窗口覆盖率生产核验 0 缺失。
    include_filter = _highlights_summary_cluster_filter(schema, cluster_alias)
    if recent_days > 0:
        cluster_sort_expr = (
            f"COALESCE({cluster_alias}.last_doc_at, {cluster_alias}.first_doc_at, "
            f"{cluster_alias}.last_updated_at, now())"
        )
        return f"""
      AND (
        {cluster_sort_expr} < now() - ({recent_days}::int * interval '1 day')
        OR {include_filter}
      )
    """
    return f"""
      AND {include_filter}
    """


def _highlights_read_model_active_version(conn: Any, schema: str) -> dict[str, Any] | None:
    active = conn.execute(
        f"""SELECT v.version_id::text AS version_id,
                   v.generated_at,
                   v.completed_at,
                   v.max_cluster_updated_at,
                   v.window_days,
                   v.min_github_stars,
                   v.meta_json,
                   sc.max_sort_at
              FROM {schema}.highlights_read_model_state st
              JOIN {schema}.highlights_read_model_versions v
                ON v.version_id = st.active_version_id
              LEFT JOIN {schema}.highlights_scopes sc
                ON sc.version_id = st.active_version_id
               AND sc.scope_key = %(scope_key)s
             WHERE st.key = %(state_key)s
               AND v.status = 'complete'""",
        {
            "scope_key": _highlights_scope_key(dimension="all"),
            "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
        },
    ).fetchone()
    return dict(active) if active else None


def _highlights_read_model_delta_checkpoint(active: dict[str, Any]) -> Any:
    meta = _json_value(active.get("meta_json"))
    checkpoint = None
    if isinstance(meta, dict):
        checkpoint = meta.get("last_delta_checkpoint_at")
    return (
        checkpoint
        or active.get("completed_at")
        or active.get("generated_at")
        or active.get("max_cluster_updated_at")
        or active.get("max_sort_at")
    )


def _trigger_highlights_read_model_self_heal(*, reason: str, min_interval_sec: int = 60) -> dict[str, Any]:
    global _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT
    if not _highlights_self_heal_enabled():
        return {"triggered": False, "skipped": "disabled"}
    with _HIGHLIGHTS_READ_MODEL_SELF_HEAL_LOCK:
        if _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT:
            return {"triggered": False, "skipped": "in_flight"}
        _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT = True

    def _worker() -> None:
        global _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT
        try:
            result = refresh_highlights_read_model_if_stale(min_interval_sec=min_interval_sec)
            print(
                f"[highlights-read-model] self-heal reason={reason}: {result}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[highlights-read-model] self-heal reason={reason} failed: {exc!r}",
                flush=True,
            )
        finally:
            with _HIGHLIGHTS_READ_MODEL_SELF_HEAL_LOCK:
                _HIGHLIGHTS_READ_MODEL_SELF_HEAL_IN_FLIGHT = False

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"highlights-read-model-self-heal:{reason}",
    ).start()
    return {"triggered": True, "min_interval_sec": int(min_interval_sec)}


def _highlights_category_sql(item_alias: str) -> str:
    raw = f"split_part(coalesce({item_alias}.ai_category, ''), '[', 1)"
    return f"""CASE {raw}
                 WHEN 'ai_tools' THEN 'efficiency_tools'
                 WHEN 'tools' THEN 'efficiency_tools'
                 WHEN 'insights' THEN 'tech'
                 ELSE {raw}
               END"""


def _highlights_category_priority_sql(category_expr: str) -> str:
    cases = "\n".join(
        f"WHEN '{category_id}' THEN {idx}"
        for idx, category_id in enumerate(ACTIVE_CATEGORY_IDS)
        if category_id != "other"
    )
    return f"CASE {category_expr} {cases} ELSE 999 END"


def _highlights_scope_item_order_sql(alias: str = "si") -> str:
    return f"{alias}.sort_at DESC NULLS LAST, {alias}.cluster_id DESC"


def _sync_highlight_cluster_decisions(
    conn: Any,
    schema: str,
    *,
    window_days: int,
    min_github_stars: int,
    checkpoint_at: Any | None = None,
    delta_cluster_table: str | None = None,
) -> None:
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, int(min_github_stars), "c")
    delta_join = ""
    checkpoint_filter = ""
    params: dict[str, Any] = {
        "window_days": max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365)),
    }
    if delta_cluster_table:
        delta_join = f"JOIN {delta_cluster_table} decision_delta ON decision_delta.cluster_id = c.id"
    elif checkpoint_at:
        checkpoint_filter = f"""AND (
                    c.last_updated_at > %(checkpoint_at)s::timestamptz
                 OR EXISTS (
                       SELECT 1
                         FROM {schema}.cluster_items ci_delta
                         JOIN {schema}.items i_delta ON i_delta.id = ci_delta.item_id
                        WHERE ci_delta.cluster_id = c.id
                          AND i_delta.highlight_scored_at > %(checkpoint_at)s::timestamptz
                    )
                  )"""
        params["checkpoint_at"] = checkpoint_at
    conn.execute(
        f"""WITH visible_clusters AS (
               SELECT c.id AS cluster_id,
                      c.ai_title,
                      c.ai_summary,
                      c.doc_count,
                      c.unique_source_count,
                      c.first_doc_at,
                      c.last_doc_at,
                      c.last_updated_at,
                      COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                 FROM {schema}.clusters c
                 {delta_join}
                WHERE c.is_visible_in_feed = true
                  AND c.published_at IS NOT NULL
                  AND coalesce(c.archived, false) = false
                  AND c.merged_into IS NULL
                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                  {checkpoint_filter}
                  {public_filter}
                  {github_filter}
             ),
             members AS (
               SELECT vc.cluster_id,
                      ci.item_id,
                      COALESCE(ci.is_primary_source, false) AS is_primary_source,
                      ci.rank_in_cluster,
                      i.highlight_verdict,
                      i.highlight_value_path,
                      i.highlight_uncertainty,
                      i.highlight_include_in_highlights,
                      i.highlight_reason,
                      i.highlight_scores,
                      i.highlight_prompt_version,
                      i.highlight_model,
                      i.highlight_last_error,
                      CASE
                        WHEN i.highlight_verdict = 'featured'
                         AND i.highlight_include_in_highlights IS TRUE THEN 'featured'
                        WHEN i.highlight_verdict = 'borderline'
                         AND i.highlight_include_in_highlights IS TRUE THEN 'positive_borderline'
                        WHEN i.highlight_verdict = 'borderline' THEN 'risk_borderline'
                        WHEN i.highlight_verdict = 'drop' THEN 'drop'
                        ELSE 'pending'
                      END AS item_cluster_verdict
                 FROM visible_clusters vc
                 JOIN {schema}.cluster_items ci ON ci.cluster_id = vc.cluster_id
                 JOIN {schema}.items i ON i.id = ci.item_id
             ),
             counts AS (
               SELECT cluster_id,
                      bool_or(highlight_include_in_highlights IS TRUE) AS has_include,
                      bool_or(item_cluster_verdict = 'featured') AS has_featured,
                      bool_or(item_cluster_verdict = 'positive_borderline') AS has_positive_borderline,
                      bool_or(item_cluster_verdict = 'risk_borderline') AS has_risk_borderline,
                      bool_or(item_cluster_verdict = 'pending') AS has_pending,
                      jsonb_build_object(
                        'featured', count(*) FILTER (WHERE item_cluster_verdict = 'featured'),
                        'positive_borderline', count(*) FILTER (WHERE item_cluster_verdict = 'positive_borderline'),
                        'risk_borderline', count(*) FILTER (WHERE item_cluster_verdict = 'risk_borderline'),
                        'drop', count(*) FILTER (WHERE item_cluster_verdict = 'drop'),
                        'pending', count(*) FILTER (WHERE item_cluster_verdict = 'pending')
                      ) AS verdict_counts_json
                 FROM members
                GROUP BY cluster_id
             ),
             best_member AS (
               SELECT *,
                      row_number() OVER (
                        PARTITION BY cluster_id
                        ORDER BY CASE item_cluster_verdict
                                   WHEN 'featured' THEN 1
                                   WHEN 'positive_borderline' THEN 2
                                   WHEN 'risk_borderline' THEN 3
                                   WHEN 'pending' THEN 4
                                   ELSE 5
                                 END,
                                 is_primary_source DESC,
                                 rank_in_cluster ASC NULLS LAST,
                                 item_id DESC
                      ) AS rn
                 FROM members
             ),
             quality AS (
               -- v25.0 F-B 质量因子：include 成员三核心维度分归一（importance+substance+novelty）/9
               SELECT cluster_id,
                      count(*) AS scored_include_count,
                      max(item_q) AS max_q,
                      avg(item_q) AS avg_q,
                      max((highlight_scores->'v26'->>'score10')::numeric) AS max_flag_score10
                 FROM (
                   SELECT cluster_id,
                          highlight_scores,
                          COALESCE(
                            (highlight_scores->'v26'->>'score10')::numeric / 10.0,
                            (
                              (highlight_scores->>'importance')::numeric
                            + (highlight_scores->>'substance')::numeric
                            + (highlight_scores->>'novelty')::numeric
                            ) / 9.0
                          ) AS item_q
                     FROM members
                    WHERE highlight_include_in_highlights IS TRUE
                      AND (
                            (
                              highlight_scores ? 'importance'
                              AND highlight_scores ? 'substance'
                              AND highlight_scores ? 'novelty'
                            )
                            OR (
                              highlight_scores ? 'v26'
                              AND highlight_scores->'v26'->>'score10' IS NOT NULL
                            )
                          )
                 ) scored
                GROUP BY cluster_id
             ),
             decisions AS (
               SELECT vc.cluster_id,
                      CASE
                        WHEN COALESCE(c.has_include, false) THEN 'included'
                        WHEN COALESCE(c.has_pending, true) THEN 'pending'
                        ELSE 'excluded'
                      END AS decision,
                      CASE
                        WHEN COALESCE(c.has_featured, false) THEN 'featured'
                        WHEN COALESCE(c.has_positive_borderline, false) THEN 'positive_borderline'
                        WHEN COALESCE(c.has_pending, true) THEN 'pending'
                        WHEN COALESCE(c.has_risk_borderline, false) THEN 'risk_borderline'
                        ELSE 'drop'
                      END AS cluster_verdict,
                      bm.item_id AS deciding_item_id,
                      COALESCE(NULLIF(bm.highlight_reason, ''), bm.highlight_last_error, '') AS reason,
                      COALESCE(c.verdict_counts_json, '{{}}'::jsonb) AS verdict_counts_json,
                      bm.highlight_prompt_version AS prompt_version,
                      bm.highlight_model AS model,
                      CASE
                        WHEN COALESCE(c.has_include, false) AND COALESCE(q.scored_include_count, 0) > 0 THEN
                          round((
                            (
                              (q.scored_include_count * ({HIGHLIGHT_SCORE_W_MAX} * q.max_q + {HIGHLIGHT_SCORE_W_AVG} * q.avg_q)
                               + {HIGHLIGHT_SCORE_SHRINK_K} * {HIGHLIGHT_SCORE_PRIOR})
                              / (q.scored_include_count + {HIGHLIGHT_SCORE_SHRINK_K})
                            )
                            * ln(1 + GREATEST(COALESCE(vc.unique_source_count, 1), 1))
                            / ln(1 + {HIGHLIGHT_SCORE_EVIDENCE_NORM_SOURCES})
                            * 100
                          )::numeric, 2)
                        ELSE NULL
                      END AS highlight_score,
                      NULLIF(
                        (
                          CASE
                            WHEN COALESCE(c.has_include, false) AND COALESCE(q.scored_include_count, 0) > 0 THEN
                              jsonb_build_object(
                                'max_q', round(q.max_q::numeric, 4),
                                'avg_q', round(q.avg_q::numeric, 4),
                                'scored_include_count', q.scored_include_count,
                                'unique_source_count', COALESCE(vc.unique_source_count, 0)
                              )
                            ELSE '{{}}'::jsonb
                          END
                        ) || jsonb_strip_nulls(jsonb_build_object(
                          'max_flag_score10', q.max_flag_score10
                        )),
                        '{{}}'::jsonb
                      ) AS score_inputs,
                      jsonb_strip_nulls(jsonb_build_object(
                        'cluster_id', vc.cluster_id,
                        'ai_title', vc.ai_title,
                        'ai_summary', vc.ai_summary,
                        'doc_count', vc.doc_count,
                        'unique_source_count', vc.unique_source_count,
                        'first_doc_at', vc.first_doc_at,
                        'last_doc_at', vc.last_doc_at,
                        'last_updated_at', vc.last_updated_at,
                        'sort_at', vc.sort_at
                      )) AS snapshot_json
                 FROM visible_clusters vc
                 LEFT JOIN counts c ON c.cluster_id = vc.cluster_id
                 LEFT JOIN quality q ON q.cluster_id = vc.cluster_id
                 LEFT JOIN best_member bm ON bm.cluster_id = vc.cluster_id AND bm.rn = 1
             )
             INSERT INTO {schema}.highlight_cluster_decisions AS target (
               cluster_id, decision, cluster_verdict, deciding_item_id,
               reason, verdict_counts_json, prompt_version, model,
               highlight_score, score_inputs,
               decided_at, updated_at, snapshot_json
             )
             SELECT cluster_id, decision, cluster_verdict, deciding_item_id,
                    reason, verdict_counts_json, prompt_version, model,
                    highlight_score, score_inputs,
                    now(), now(), snapshot_json
               FROM decisions
             ON CONFLICT (cluster_id) DO UPDATE SET
               decision = excluded.decision,
               cluster_verdict = excluded.cluster_verdict,
               deciding_item_id = excluded.deciding_item_id,
               reason = excluded.reason,
               verdict_counts_json = excluded.verdict_counts_json,
               prompt_version = excluded.prompt_version,
               model = excluded.model,
               highlight_score = excluded.highlight_score,
               score_inputs = excluded.score_inputs,
               decided_at = excluded.decided_at,
               updated_at = excluded.updated_at,
               snapshot_json = excluded.snapshot_json
             WHERE target.decision IS DISTINCT FROM excluded.decision
                OR target.cluster_verdict IS DISTINCT FROM excluded.cluster_verdict
                OR target.deciding_item_id IS DISTINCT FROM excluded.deciding_item_id
                OR target.reason IS DISTINCT FROM excluded.reason
                OR target.verdict_counts_json IS DISTINCT FROM excluded.verdict_counts_json
                OR target.prompt_version IS DISTINCT FROM excluded.prompt_version
                OR target.model IS DISTINCT FROM excluded.model
                OR target.highlight_score IS DISTINCT FROM excluded.highlight_score
                OR target.score_inputs IS DISTINCT FROM excluded.score_inputs
                OR target.snapshot_json IS DISTINCT FROM excluded.snapshot_json""",
        params,
    )


def refresh_highlights_read_model_delta_in_place(
    *,
    window_days: int = HIGHLIGHTS_READ_MODEL_WINDOW_DAYS,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Apply changed highlight clusters to the active read model in place."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    safe_window_days = max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365))
    safe_min_github_stars = int(min_github_stars)
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, safe_min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=_highlights_display_threshold(),
    )
    category_expr = _highlights_category_sql("i")
    category_priority = _highlights_category_priority_sql("category")
    active_categories = [category_id for category_id in ACTIVE_CATEGORY_IDS if category_id != "other"]
    refresh_timeout_ms = _env_int(
        _runtime_env(),
        HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
        HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
        min_value=60000,
    )
    t0 = time.time()
    timings_ms: dict[str, int] = {}
    current_step = "init"

    def _record_step(step: str, started_at: float) -> None:
        timings_ms[step] = int((time.time() - started_at) * 1000)

    current_step = "read_active_version"
    step_t0 = time.time()
    with connect() as conn:
        _set_short_statement_timeout(conn, refresh_timeout_ms)
        active = _highlights_read_model_active_version(conn, schema)
    if not active or not active.get("version_id"):
        return refresh_highlights_read_model(
            window_days=safe_window_days,
            min_github_stars=safe_min_github_stars,
        )
    active_version_id = str(active["version_id"])
    checkpoint_at = _highlights_read_model_delta_checkpoint(active)
    if not checkpoint_at:
        return refresh_highlights_read_model(
            window_days=safe_window_days,
            min_github_stars=safe_min_github_stars,
        )
    _record_step(current_step, step_t0)

    delta_clusters_sql = f"""WITH candidate_delta_clusters AS (
                               SELECT c.id AS cluster_id,
                                      c.last_updated_at AS delta_checkpoint_at
                                 FROM {schema}.clusters c
                                WHERE c.is_visible_in_feed = true
                                  AND c.published_at IS NOT NULL
                                  AND coalesce(c.archived, false) = false
                                  AND c.merged_into IS NULL
                                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                                  AND c.last_updated_at > %(checkpoint_at)s::timestamptz
                                  {public_filter}
                                  {github_filter}
                               UNION ALL
                               SELECT c.id AS cluster_id,
                                      max(i_delta.highlight_scored_at) AS delta_checkpoint_at
                                 FROM {schema}.items i_delta
                                 JOIN {schema}.cluster_items ci_delta
                                   ON ci_delta.item_id = i_delta.id
                                 JOIN {schema}.clusters c
                                   ON c.id = ci_delta.cluster_id
                                WHERE i_delta.highlight_scored_at > %(checkpoint_at)s::timestamptz
                                  AND c.is_visible_in_feed = true
                                  AND c.published_at IS NOT NULL
                                  AND coalesce(c.archived, false) = false
                                  AND c.merged_into IS NULL
                                  AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                                  {public_filter}
                                  {github_filter}
                                GROUP BY c.id
                             )
                             SELECT cluster_id,
                                    max(delta_checkpoint_at) AS delta_checkpoint_at
                               FROM candidate_delta_clusters
                              GROUP BY cluster_id"""

    delta_scope_cte = f"""WITH base_clusters AS (
                       SELECT c.id AS cluster_id,
                              c.ai_title,
                              c.ai_summary,
                              c.doc_count,
                              c.unique_source_count,
                              c.first_doc_at,
                              c.last_doc_at,
                              c.platforms_json,
                              COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                              c.live_version,
                              c.last_updated_at,
                              COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                         FROM {schema}.clusters c
                         JOIN pg_temp.highlights_read_model_delta_clusters dc
                           ON dc.cluster_id = c.id
                         LEFT JOIN LATERAL (
                           SELECT i_cover.cover_url
                             FROM {schema}.cluster_items ci_cover
                             JOIN {schema}.items i_cover ON i_cover.id = ci_cover.item_id
                            WHERE ci_cover.cluster_id = c.id
                              AND NULLIF(i_cover.cover_url, '') IS NOT NULL
                              AND i_cover.platform <> 'manual'
                              AND i_cover.user_id IS NULL
                            ORDER BY COALESCE(ci_cover.is_primary_source, false) DESC,
                                     ci_cover.rank_in_cluster ASC NULLS LAST
                            LIMIT 1
                         ) event_cover ON true
                        WHERE c.is_visible_in_feed = true
                          AND c.published_at IS NOT NULL
                          AND coalesce(c.archived, false) = false
                          AND c.merged_into IS NULL
                          AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                          {public_filter}
                          {github_filter}
                          {verdict_filter}
                          {display_filter}
                     ),
                     source_members AS (
                       SELECT ci.cluster_id,
                              ci.source_identity,
                              ci.rank_in_cluster,
                              COALESCE(ci.is_primary_source, false) AS is_primary_source,
                              i.id AS item_id,
                              i.platform,
                              i.author_name,
                              i.source,
                              i.url,
                              i.ai_category,
                              i.published_at,
                              i.fetched_at,
                              {category_expr} AS category
                         FROM {schema}.cluster_items ci
                         JOIN {schema}.items i ON i.id = ci.item_id
                         JOIN base_clusters b ON b.cluster_id = ci.cluster_id
                     ),
                     category_counts AS (
                       SELECT cluster_id, category, count(*) AS n
                         FROM source_members
                        WHERE category = ANY(%(active_categories)s::text[])
                        GROUP BY cluster_id, category
                     ),
                     category_ranked AS (
                       SELECT cluster_id,
                              category,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY n DESC,
                                         {category_priority},
                                         category ASC
                              ) AS rn
                         FROM category_counts
                     ),
                     source_dedup AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              is_primary_source,
                              rank_in_cluster,
                              published_at,
                              fetched_at,
                              row_number() OVER (
                                PARTITION BY cluster_id,
                                             COALESCE(
                                               source_identity,
                                               url,
                                               platform || ':' || COALESCE(author_name, source, item_id::text)
                                             )
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                         item_id DESC
                              ) AS identity_rn
                         FROM source_members
                     ),
                     source_ranked AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST
                              ) AS preview_rn
                         FROM source_dedup
                        WHERE identity_rn = 1
                     ),
                     source_preview AS (
                       SELECT cluster_id,
                              jsonb_agg(
                                jsonb_strip_nulls(jsonb_build_object(
                                  'platform', platform,
                                  'author', author_name,
                                  'source', source
                                ))
                                ORDER BY preview_rn
                              ) FILTER (WHERE preview_rn <= 3) AS source_preview
                         FROM source_ranked
                        GROUP BY cluster_id
                     ),
                     cluster_cards AS (
                       SELECT b.cluster_id,
                              b.sort_at,
                              b.last_updated_at,
                              cr.category,
                              jsonb_strip_nulls(jsonb_build_object(
                                'id', b.cluster_id,
                                'ai_title', b.ai_title,
                                'ai_summary', b.ai_summary,
                                'doc_count', b.doc_count,
                                'unique_source_count', b.unique_source_count,
                                'category', cr.category,
                                'source_preview', COALESCE(sp.source_preview, '[]'::jsonb),
                                'first_doc_at', b.first_doc_at,
                                'last_doc_at', b.last_doc_at,
                                'platforms', COALESCE(b.platforms_json, '[]'::jsonb),
                                'cover_url', b.cover_url,
                                'live_version', b.live_version
                              )) AS card_json
                         FROM base_clusters b
                         LEFT JOIN category_ranked cr
                                ON cr.cluster_id = b.cluster_id
                               AND cr.rn = 1
                         LEFT JOIN source_preview sp ON sp.cluster_id = b.cluster_id
                     ),
                     scope_rows AS (
                       SELECT %(scope_key_all)s::text AS scope_key,
                              'all'::text AS dimension,
                              ''::text AS value,
                              cluster_id,
                              sort_at,
                              last_updated_at,
                              card_json
                         FROM cluster_cards
                       UNION ALL
                       SELECT 'category:' || category AS scope_key,
                              'category'::text AS dimension,
                              category AS value,
                              cluster_id,
                              sort_at,
                              last_updated_at,
                              card_json
                         FROM cluster_cards
                        WHERE category IS NOT NULL
                          AND category != ''
                     )"""
    params = {
        "active_version_id": active_version_id,
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "checkpoint_at": checkpoint_at,
        "active_categories": active_categories,
        "scope_key_all": "all",
        "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
    }
    with connect() as conn:
        try:
            _set_short_statement_timeout(conn, refresh_timeout_ms)
            current_step = "materialize_delta_clusters"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_clusters")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_delta_clusters ON COMMIT DROP AS
                    {delta_clusters_sql}""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_clusters")
            delta_cluster_row = conn.execute(
                """SELECT count(*) AS clusters,
                          max(delta_checkpoint_at) AS max_delta_checkpoint_at
                     FROM pg_temp.highlights_read_model_delta_clusters"""
            ).fetchone()
            delta_clusters = int((delta_cluster_row or {}).get("clusters") or 0)
            if delta_clusters <= 0:
                conn.commit()
                return {
                    "ok": True,
                    "skipped": "no_delta",
                    "mode": "delta_in_place",
                    "active_version_id": active_version_id,
                    "checkpoint_at": _timestamp_value(checkpoint_at),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "timings_ms": timings_ms,
                }
            delta_max_checkpoint_at = (delta_cluster_row or {}).get("max_delta_checkpoint_at")
            _record_step(current_step, step_t0)

            current_step = "sync_cluster_decisions"
            step_t0 = time.time()
            _sync_highlight_cluster_decisions(
                conn,
                schema,
                window_days=safe_window_days,
                min_github_stars=safe_min_github_stars,
                delta_cluster_table="pg_temp.highlights_read_model_delta_clusters",
            )
            _record_step(current_step, step_t0)

            current_step = "materialize_delta_scope_rows"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_scope_rows")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_delta_scope_rows ON COMMIT DROP AS
                    {delta_scope_cte}
                    SELECT scope_key, dimension, value, cluster_id, sort_at,
                           last_updated_at, card_json
                      FROM scope_rows""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_scope_rows")
            delta_row = conn.execute(
                """SELECT count(*) AS scope_rows
                     FROM pg_temp.highlights_read_model_delta_scope_rows"""
            ).fetchone()
            delta_scope_rows = int((delta_row or {}).get("scope_rows") or 0)
            _record_step(current_step, step_t0)

            current_step = "materialize_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_affected_scopes")
            conn.execute(
                f"""CREATE TEMP TABLE highlights_read_model_affected_scopes ON COMMIT DROP AS
                   SELECT DISTINCT scope_key
                     FROM pg_temp.highlights_read_model_delta_scope_rows
                   UNION
                   SELECT DISTINCT si.scope_key
                     FROM {schema}.highlights_scope_items si
                     JOIN pg_temp.highlights_read_model_delta_clusters dc
                       ON dc.cluster_id = si.cluster_id
                    WHERE si.version_id = %(active_version_id)s::uuid""",
                params,
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_affected_scopes")
            _record_step(current_step, step_t0)

            current_step = "delete_obsolete_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""DELETE FROM {schema}.highlights_scope_items si
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND EXISTS (
                             SELECT 1
                               FROM pg_temp.highlights_read_model_delta_clusters dc
                              WHERE dc.cluster_id = si.cluster_id
                           )
                       AND NOT EXISTS (
                             SELECT 1
                               FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                              WHERE dsr.scope_key = si.scope_key
                                AND dsr.cluster_id = si.cluster_id
                           )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "update_existing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.highlights_scope_items si
                       SET sort_at = dsr.sort_at,
                           card_json = dsr.card_json
                      FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                     WHERE si.version_id = %(active_version_id)s::uuid
                       AND si.scope_key = dsr.scope_key
                       AND si.cluster_id = dsr.cluster_id
                       AND (
                            si.sort_at IS DISTINCT FROM dsr.sort_at
                         OR si.card_json IS DISTINCT FROM dsr.card_json
                       )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "insert_missing_scope_items"
            step_t0 = time.time()
            conn.execute(
                f"""WITH missing AS (
                       SELECT dsr.scope_key, dsr.cluster_id, dsr.sort_at, dsr.card_json
                         FROM pg_temp.highlights_read_model_delta_scope_rows dsr
                        WHERE NOT EXISTS (
                                SELECT 1
                                  FROM {schema}.highlights_scope_items si
                                 WHERE si.version_id = %(active_version_id)s::uuid
                                   AND si.scope_key = dsr.scope_key
                                   AND si.cluster_id = dsr.cluster_id
                              )
                     ),
                     scope_max_rank AS (
                       SELECT si.scope_key, max(si.rank) AS max_rank
                         FROM {schema}.highlights_scope_items si
                        WHERE si.version_id = %(active_version_id)s::uuid
                          AND EXISTS (
                                SELECT 1
                                  FROM pg_temp.highlights_read_model_affected_scopes a
                                 WHERE a.scope_key = si.scope_key
                              )
                        GROUP BY si.scope_key
                     ),
                     ranked AS (
                       SELECT m.scope_key, m.cluster_id, m.sort_at, m.card_json,
                              COALESCE(smr.max_rank, 0) + row_number() OVER (
                                PARTITION BY m.scope_key
                                ORDER BY m.sort_at DESC NULLS LAST,
                                         m.cluster_id DESC
                              ) AS append_rank
                         FROM missing m
                         LEFT JOIN scope_max_rank smr
                           ON smr.scope_key = m.scope_key
                     )
                     INSERT INTO {schema}.highlights_scope_items (
                       version_id, scope_key, rank, cluster_id, sort_at, card_json
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            append_rank::integer,
                            cluster_id,
                            sort_at,
                            card_json
                       FROM ranked""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "upsert_affected_scopes"
            step_t0 = time.time()
            conn.execute("DROP TABLE IF EXISTS pg_temp.highlights_read_model_delta_scope_meta")
            conn.execute(
                """CREATE TEMP TABLE highlights_read_model_delta_scope_meta ON COMMIT DROP AS
                   SELECT scope_key, max(dimension) AS dimension, max(value) AS value
                     FROM pg_temp.highlights_read_model_delta_scope_rows
                    GROUP BY scope_key"""
            )
            conn.execute("ANALYZE pg_temp.highlights_read_model_delta_scope_meta")
            conn.execute(
                f"""WITH affected_scope_stats AS (
                       SELECT a.scope_key,
                              COALESCE(max(dsm.dimension), max(sc.dimension)) AS dimension,
                              COALESCE(max(dsm.value), max(sc.value), '') AS value,
                              count(si.cluster_id)::integer AS total_count,
                              max(si.sort_at) AS max_sort_at
                         FROM pg_temp.highlights_read_model_affected_scopes a
                         LEFT JOIN {schema}.highlights_scopes sc
                           ON sc.version_id = %(active_version_id)s::uuid
                          AND sc.scope_key = a.scope_key
                         LEFT JOIN pg_temp.highlights_read_model_delta_scope_meta dsm
                           ON dsm.scope_key = a.scope_key
                         LEFT JOIN {schema}.highlights_scope_items si
                           ON si.version_id = %(active_version_id)s::uuid
                          AND si.scope_key = a.scope_key
                        GROUP BY a.scope_key
                     )
                     INSERT INTO {schema}.highlights_scopes (
                       version_id, scope_key, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(active_version_id)s::uuid,
                            scope_key,
                            dimension,
                            value,
                            total_count,
                            max_sort_at,
                            now()
                       FROM affected_scope_stats
                      WHERE total_count > 0
                     ON CONFLICT (version_id, scope_key) DO UPDATE SET
                       dimension = excluded.dimension,
                       value = excluded.value,
                       total_count = excluded.total_count,
                       max_sort_at = excluded.max_sort_at,
                       generated_at = excluded.generated_at""",
                params,
            )
            conn.execute(
                f"""DELETE FROM {schema}.highlights_scopes sc
                      USING pg_temp.highlights_read_model_affected_scopes a
                     WHERE sc.version_id = %(active_version_id)s::uuid
                       AND sc.scope_key = a.scope_key
                       AND NOT EXISTS (
                             SELECT 1
                               FROM {schema}.highlights_scope_items si
                              WHERE si.version_id = sc.version_id
                                AND si.scope_key = sc.scope_key
                           )""",
                params,
            )
            _record_step(current_step, step_t0)

            current_step = "update_active_version"
            step_t0 = time.time()
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_versions
                       SET completed_at = now(),
                           max_cluster_updated_at = (
                             SELECT max(max_sort_at)
                               FROM {schema}.highlights_scopes
                              WHERE version_id = %(active_version_id)s::uuid
                           ),
                           meta_json = COALESCE(meta_json, '{{}}'::jsonb) || jsonb_build_object(
                             'read_model', %(read_model)s::text,
                             'last_delta_mode', 'in_place',
                             'last_delta_at', now(),
                             'last_delta_checkpoint_at', %(delta_max_checkpoint_at)s::timestamptz
                           )
                     WHERE version_id = %(active_version_id)s::uuid""",
                {
                    **params,
                    "read_model": HIGHLIGHTS_READ_MODEL_VERSION,
                    "delta_max_checkpoint_at": delta_max_checkpoint_at,
                },
            )
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_state
                       SET updated_at = now()
                     WHERE key = %(state_key)s
                       AND active_version_id = %(active_version_id)s::uuid""",
                params,
            )

            current_step = "count_scope_items"
            step_t0 = time.time()
            scope_item_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = %(active_version_id)s::uuid""",
                params,
            ).fetchone()
            _record_step(current_step, step_t0)

            current_step = "commit"
            step_t0 = time.time()
            conn.commit()
            _record_step(current_step, step_t0)
        except Exception as exc:
            _rollback_safely(conn)
            raise RemoteDBError(f"highlights read model in-place delta refresh failed at {current_step}: {exc}") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "mode": "delta_in_place",
        "version_id": active_version_id,
        "delta_clusters": delta_clusters,
        "delta_scope_rows": delta_scope_rows,
        "active_checkpoint_at": _timestamp_value(delta_max_checkpoint_at),
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "timings_ms": timings_ms,
    }


def refresh_highlights_read_model_if_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    global _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT, _HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    # P-C insurance: never refresh the highlights read model while a fetch run is
    # still in flight. During a run, clusters are published in one batch only at
    # publish_run() (end of run); a mid-run refresh (e.g. request-path self-heal)
    # can advance the delta checkpoint past clusters that are visible but not yet
    # fully scored, and the single scalar checkpoint cannot recover them. The
    # post-fetch refresh is triggered *after* finish_fetch_run marks the run done,
    # so this guard does not block that legitimate path. Fail open (proceed) if the
    # running-run probe itself errors — never let a transient DB error stall refresh.
    if _highlights_refresh_skip_during_fetch_enabled():
        try:
            fetch_running = has_recent_running_fetch_remote()
        except Exception:
            fetch_running = False
        if fetch_running:
            return {"ok": True, "skipped": "fetch_running"}
    min_interval = max(0, int(min_interval_sec))
    now = time.monotonic()
    with _HIGHLIGHTS_READ_MODEL_REFRESH_LOCK:
        failures = _HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES
        effective_interval = _info_read_model_refresh_effective_interval(min_interval, failures)
        age = (
            now - _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
            if _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT
            else None
        )
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
        _HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = now
    try:
        if _highlights_read_model_incremental_enabled():
            result = refresh_highlights_read_model_delta_in_place()
        else:
            result = refresh_highlights_read_model()
    except Exception:
        with _HIGHLIGHTS_READ_MODEL_REFRESH_LOCK:
            _HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES += 1
        raise
    with _HIGHLIGHTS_READ_MODEL_REFRESH_LOCK:
        _HIGHLIGHTS_READ_MODEL_REFRESH_CONSECUTIVE_FAILURES = 0
    return result


def _highlights_sort_tuple(row: dict[str, Any] | None) -> tuple[float, int]:
    if not row:
        return (float("-inf"), 0)
    raw_id = row.get("id", row.get("cluster_id", 0))
    try:
        cluster_id = int(raw_id or 0)
    except (TypeError, ValueError):
        cluster_id = 0
    return (sort_key(row.get("sort_at")), cluster_id)


def highlights_read_model_freshness(
    *,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Compare the active highlights read model with the live visible top row."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "enabled": False, "stale": False, "skipped": "disabled"}
    schema = remote_schema()
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, int(min_github_stars), "c")
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=_highlights_display_threshold(),
    )
    with connect() as conn:
        _set_short_statement_timeout(conn, 2500)
        try:
            conn.execute("SET TRANSACTION READ ONLY")
        except Exception:
            _rollback_safely(conn)
        active = conn.execute(
            f"""SELECT st.active_version_id::text AS active_version_id,
                       sc.max_sort_at,
                       top.cluster_id,
                       top.sort_at
                  FROM {schema}.highlights_read_model_state st
                  JOIN {schema}.highlights_read_model_versions v
                    ON v.version_id = st.active_version_id
                  LEFT JOIN {schema}.highlights_scopes sc
                    ON sc.version_id = st.active_version_id
                   AND sc.scope_key = %(scope_key)s
                  LEFT JOIN LATERAL (
                    SELECT cluster_id, sort_at
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = st.active_version_id
                       AND scope_key = %(scope_key)s
                     ORDER BY {_highlights_scope_item_order_sql("highlights_scope_items")}
                     LIMIT 1
                  ) top ON true
                 WHERE st.key = %(state_key)s
                   AND v.status = 'complete'""",
            {
                "scope_key": _highlights_scope_key(dimension="all"),
                "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
            },
        ).fetchone()
        latest = conn.execute(
            f"""SELECT c.id,
                       COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                  FROM {schema}.clusters c
                 WHERE c.is_visible_in_feed = true
                   AND c.published_at IS NOT NULL
                   AND coalesce(c.archived, false) = false
                   AND c.merged_into IS NULL
                   AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                   {public_filter}
                   {github_filter}
                   {display_filter}
                 ORDER BY sort_at DESC NULLS LAST,
                          c.id DESC
                 LIMIT 1""",
            {"window_days": HIGHLIGHTS_READ_MODEL_WINDOW_DAYS},
        ).fetchone()
    active_dict = dict(active) if active else None
    latest_dict = dict(latest) if latest else None
    stale = False
    reason = "data_fresh"
    if latest_dict and not active_dict:
        stale = True
        reason = "missing_active_version"
    elif latest_dict and _highlights_sort_tuple(latest_dict) > _highlights_sort_tuple(active_dict):
        stale = True
        reason = "live_top_newer"
    return {
        "ok": True,
        "enabled": True,
        "stale": stale,
        "reason": reason,
        "active_version_id": (active_dict or {}).get("active_version_id"),
        "active_top_cluster_id": (active_dict or {}).get("cluster_id"),
        "active_top_sort_at": to_utc_iso((active_dict or {}).get("sort_at")),
        "latest_cluster_id": (latest_dict or {}).get("id"),
        "latest_sort_at": to_utc_iso((latest_dict or {}).get("sort_at")),
    }


def refresh_highlights_read_model_if_data_stale(*, min_interval_sec: int = 600) -> dict[str, Any]:
    freshness = highlights_read_model_freshness()
    if not freshness.get("stale"):
        return {**freshness, "skipped": freshness.get("reason") or "data_fresh"}
    refreshed = refresh_highlights_read_model_if_stale(min_interval_sec=min_interval_sec)
    return {**freshness, "refresh": refreshed}


def refresh_highlights_read_model(
    *,
    window_days: int = HIGHLIGHTS_READ_MODEL_WINDOW_DAYS,
    min_github_stars: int = HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
) -> dict[str, Any]:
    """Build a version-swapped read model for the 精选 tab event timeline."""
    if not _highlights_read_model_enabled():
        return {"ok": True, "skipped": "disabled"}
    schema = remote_schema()
    version_id = str(uuid.uuid4())
    safe_window_days = max(1, min(int(window_days or HIGHLIGHTS_READ_MODEL_WINDOW_DAYS), 365))
    safe_min_github_stars = int(min_github_stars)
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(schema, safe_min_github_stars, "c")
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=_highlights_display_threshold(),
    )
    category_expr = _highlights_category_sql("i")
    category_priority = _highlights_category_priority_sql("category")
    active_categories = [category_id for category_id in ACTIVE_CATEGORY_IDS if category_id != "other"]
    scope_cte = f"""WITH base_clusters AS (
                       SELECT c.id AS cluster_id,
                              c.ai_title,
                              c.ai_summary,
                              c.doc_count,
                              c.unique_source_count,
                              c.first_doc_at,
                              c.last_doc_at,
                              c.platforms_json,
                              COALESCE(NULLIF(c.cover_url, ''), event_cover.cover_url) AS cover_url,
                              c.live_version,
                              c.last_updated_at,
                              COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
                         FROM {schema}.clusters c
                         LEFT JOIN LATERAL (
                           SELECT i_cover.cover_url
                             FROM {schema}.cluster_items ci_cover
                             JOIN {schema}.items i_cover ON i_cover.id = ci_cover.item_id
                            WHERE ci_cover.cluster_id = c.id
                              AND NULLIF(i_cover.cover_url, '') IS NOT NULL
                              AND i_cover.platform <> 'manual'
                              AND i_cover.user_id IS NULL
                            ORDER BY COALESCE(ci_cover.is_primary_source, false) DESC,
                                     ci_cover.rank_in_cluster ASC NULLS LAST
                            LIMIT 1
                         ) event_cover ON true
                        WHERE c.is_visible_in_feed = true
                          AND c.published_at IS NOT NULL
                          AND coalesce(c.archived, false) = false
                          AND c.merged_into IS NULL
                          AND c.last_updated_at > now() - (%(window_days)s::int * interval '1 day')
                          {public_filter}
                          {github_filter}
                          {verdict_filter}
                          {display_filter}
                     ),
                     source_members AS (
                       SELECT ci.cluster_id,
                              ci.source_identity,
                              ci.rank_in_cluster,
                              COALESCE(ci.is_primary_source, false) AS is_primary_source,
                              i.id AS item_id,
                              i.platform,
                              i.author_name,
                              i.source,
                              i.url,
                              i.ai_category,
                              i.published_at,
                              i.fetched_at,
                              {category_expr} AS category
                         FROM {schema}.cluster_items ci
                         JOIN {schema}.items i ON i.id = ci.item_id
                         JOIN base_clusters b ON b.cluster_id = ci.cluster_id
                     ),
                     category_counts AS (
                       SELECT cluster_id, category, count(*) AS n
                         FROM source_members
                        WHERE category = ANY(%(active_categories)s::text[])
                        GROUP BY cluster_id, category
                     ),
                     category_ranked AS (
                       SELECT cluster_id,
                              category,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY n DESC,
                                         {category_priority},
                                         category ASC
                              ) AS rn
                         FROM category_counts
                     ),
                     source_dedup AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              is_primary_source,
                              rank_in_cluster,
                              published_at,
                              fetched_at,
                              row_number() OVER (
                                PARTITION BY cluster_id,
                                             COALESCE(
                                               source_identity,
                                               url,
                                               platform || ':' || COALESCE(author_name, source, item_id::text)
                                             )
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST,
                                         item_id DESC
                              ) AS identity_rn
                         FROM source_members
                     ),
                     source_ranked AS (
                       SELECT cluster_id,
                              platform,
                              author_name,
                              source,
                              row_number() OVER (
                                PARTITION BY cluster_id
                                ORDER BY is_primary_source DESC,
                                         rank_in_cluster ASC NULLS LAST,
                                         COALESCE(published_at, fetched_at) DESC NULLS LAST
                              ) AS preview_rn
                         FROM source_dedup
                        WHERE identity_rn = 1
                     ),
                     source_preview AS (
                       SELECT cluster_id,
                              jsonb_agg(
                                jsonb_strip_nulls(jsonb_build_object(
                                  'platform', platform,
                                  'author', author_name,
                                  'source', source
                                ))
                                ORDER BY preview_rn
                              ) FILTER (WHERE preview_rn <= 3) AS source_preview
                         FROM source_ranked
                        GROUP BY cluster_id
                     ),
                     cluster_cards AS (
                       SELECT b.cluster_id,
                              b.sort_at,
                              cr.category,
                              jsonb_strip_nulls(jsonb_build_object(
                                'id', b.cluster_id,
                                'ai_title', b.ai_title,
                                'ai_summary', b.ai_summary,
                                'doc_count', b.doc_count,
                                'unique_source_count', b.unique_source_count,
                                'category', cr.category,
                                'source_preview', COALESCE(sp.source_preview, '[]'::jsonb),
                                'first_doc_at', b.first_doc_at,
                                'last_doc_at', b.last_doc_at,
                                'platforms', COALESCE(b.platforms_json, '[]'::jsonb),
                                'cover_url', b.cover_url,
                                'live_version', b.live_version
                              )) AS card_json
                         FROM base_clusters b
                         LEFT JOIN category_ranked cr
                                ON cr.cluster_id = b.cluster_id
                               AND cr.rn = 1
                         LEFT JOIN source_preview sp ON sp.cluster_id = b.cluster_id
                     ),
                     scope_rows AS (
                       SELECT %(scope_key_all)s::text AS scope_key,
                              'all'::text AS dimension,
                              ''::text AS value,
                              cluster_id,
                              sort_at,
                              card_json
                         FROM cluster_cards
                       UNION ALL
                       SELECT 'category:' || category AS scope_key,
                              'category'::text AS dimension,
                              category AS value,
                              cluster_id,
                              sort_at,
                              card_json
                         FROM cluster_cards
                        WHERE category IS NOT NULL
                          AND category != ''
                     )"""
    params = {
        "version_id": version_id,
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "active_categories": active_categories,
        "scope_key_all": "all",
        "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
        "meta_json": json.dumps({"read_model": HIGHLIGHTS_READ_MODEL_VERSION}),
    }
    t0 = time.time()
    with connect() as conn:
        try:
            _set_short_statement_timeout(
                conn,
                _env_int(
                    _runtime_env(),
                    HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_ENV,
                    HIGHLIGHTS_READ_MODEL_REFRESH_TIMEOUT_MS_DEFAULT,
                    min_value=60000,
                ),
            )
            conn.execute(
                f"""INSERT INTO {schema}.highlights_read_model_versions (
                       version_id, status, generated_at, window_days,
                       min_github_stars, meta_json
                     )
                     VALUES (
                       %(version_id)s::uuid, 'building', now(), %(window_days)s,
                       %(min_github_stars)s, %(meta_json)s::jsonb
                     )""",
                params,
            )
            # perf-v27 P1: decisions 同步必须先于 scope 物化——verdict_filter 现在
            # 读 highlight_cluster_decisions,若 sync 在后,本轮新打分的簇会被旧
            # 决策行漏掉一个刷新周期。(delta 路径原本就是 sync 在前,此处对齐。)
            _sync_highlight_cluster_decisions(
                conn,
                schema,
                window_days=safe_window_days,
                min_github_stars=safe_min_github_stars,
            )
            conn.execute(
                f"""{scope_cte}
                     INSERT INTO {schema}.highlights_scopes (
                       version_id, scope_key, dimension, value,
                       total_count, max_sort_at, generated_at
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            dimension,
                            value,
                            count(*)::integer,
                            max(sort_at),
                            now()
                       FROM scope_rows
                      GROUP BY scope_key, dimension, value""",
                params,
            )
            conn.execute(
                f"""{scope_cte},
                     ranked AS (
                       SELECT scope_key,
                              cluster_id,
                              sort_at,
                              card_json,
                              row_number() OVER (
                                PARTITION BY scope_key
                                ORDER BY sort_at DESC NULLS LAST,
                                         cluster_id DESC
                              ) AS rn
                         FROM scope_rows
                     )
                     INSERT INTO {schema}.highlights_scope_items (
                       version_id, scope_key, rank, cluster_id, sort_at, card_json
                     )
                     SELECT %(version_id)s::uuid,
                            scope_key,
                            rn::integer,
                            cluster_id,
                            sort_at,
                            card_json
                       FROM ranked""",
                params,
            )
            conn.execute(
                f"""UPDATE {schema}.highlights_read_model_versions
                       SET status = 'complete',
                           completed_at = now(),
                           max_cluster_updated_at = (
                             SELECT max(sort_at)
                               FROM {schema}.highlights_scope_items
                              WHERE version_id = %(version_id)s::uuid
                           )
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            )
            conn.execute(
                f"""INSERT INTO {schema}.highlights_read_model_state (key, active_version_id, updated_at)
                     VALUES (%(state_key)s, %(version_id)s::uuid, now())
                     ON CONFLICT (key) DO UPDATE SET
                       active_version_id = excluded.active_version_id,
                       updated_at = excluded.updated_at""",
                params,
            )
            scope_item_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scope_items
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            scope_row = conn.execute(
                f"""SELECT count(*) AS n
                      FROM {schema}.highlights_scopes
                     WHERE version_id = %(version_id)s::uuid""",
                params,
            ).fetchone()
            conn.execute(
                f"""DELETE FROM {schema}.highlights_read_model_versions
                     WHERE version_id NOT IN (
                       SELECT version_id
                         FROM {schema}.highlights_read_model_versions
                        ORDER BY generated_at DESC
                        LIMIT 3
                     )"""
            )
            conn.commit()
        except Exception as exc:
            _rollback_safely(conn)
            try:
                conn.execute(
                    f"""UPDATE {schema}.highlights_read_model_versions
                           SET status = 'error',
                               error_message = %(error_message)s,
                               completed_at = now()
                         WHERE version_id = %(version_id)s::uuid""",
                    {"version_id": version_id, "error_message": str(exc)[:500]},
                )
                conn.commit()
            except Exception:
                _rollback_safely(conn)
            raise RemoteDBError("highlights read model refresh failed") from exc
    clear_feed_cache_keys()
    return {
        "ok": True,
        "version_id": version_id,
        "scope_items": int((scope_item_row or {}).get("n") or 0),
        "scopes": int((scope_row or {}).get("n") or 0),
        "window_days": safe_window_days,
        "min_github_stars": safe_min_github_stars,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


_ADMIN_HIGHLIGHTS_FUNNEL_VIEWS = {"panorama", "anomaly"}
_ADMIN_HIGHLIGHTS_FUNNEL_DISPLAYS = {"all", "shown", "hidden"}
_ADMIN_HIGHLIGHTS_FUNNEL_STAGES = {
    "",
    "pending",
    "displayed",
    "blocked_scoring",
    "blocked_summary",
    "blocked_display",
}
_ADMIN_HIGHLIGHTS_FUNNEL_TIMEOUT_MS = 15_000


def _admin_highlights_funnel_days(days: int) -> int:
    safe_days = int(days)
    if safe_days not in {1, 3, 7}:
        raise ValueError("days must be 1, 3, or 7")
    return safe_days


def _admin_highlights_funnel_query(q: str | None) -> tuple[str, str]:
    query = str(q or "").strip()[:200]
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return query, f"%{escaped}%"


def _admin_highlights_funnel_tag(tag: str | None) -> str:
    safe_tag = canonicalize_category(str(tag or "").strip()) or ""
    if safe_tag and safe_tag not in {*ACTIVE_CATEGORY_IDS, "other"}:
        raise ValueError("unsupported funnel tag")
    return safe_tag


def _admin_highlights_tag_filter(alias: str) -> str:
    return f"(%(tag)s = '' OR {alias}.dominant_category = %(tag)s)"


def _admin_highlights_funnel_ctes(
    schema: str,
    *,
    display_threshold: float | None,
) -> str:
    verdict_filter = _highlights_verdict_cluster_filter(schema, "c")
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=display_threshold,
    )
    public_filter = _public_cluster_filter(schema, "c")
    github_filter = _github_display_filter(
        schema,
        HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS,
        "c",
    )
    category_sql = """/* aliases mirror src/category_taxonomy.py */
                      CASE lower(BTRIM(split_part(COALESCE(i.ai_category, ''), '[', 1)))
                        WHEN 'ai_tools' THEN 'efficiency_tools'
                        WHEN 'tools' THEN 'efficiency_tools'
                        WHEN 'insights' THEN 'tech'
                        ELSE lower(BTRIM(split_part(COALESCE(i.ai_category, ''), '[', 1)))
                      END"""
    actionable_error_filter = """i.highlight_last_error IS NOT NULL
                              AND (
                                i.highlight_error_count >= 3
                                OR i.highlight_retry_after IS NULL
                                OR i.highlight_retry_after <= now()
                              )"""
    tag_filter = _admin_highlights_tag_filter("s")
    if display_threshold is None:
        blocked_display_reason = """CASE
                     WHEN d.manual_display = 'force_hide' THEN 'manual_hide'
                     ELSE COALESCE(NULLIF(d.reason, ''), 'below_threshold')
                   END"""
    else:
        threshold_sql = repr(float(display_threshold))
        blocked_display_reason = f"""CASE
                     WHEN d.manual_display = 'force_hide' THEN 'manual_hide'
                     WHEN c.why_read IS NULL
                      AND (
                        d.manual_display = 'force_show'
                        OR NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric >= {threshold_sql}
                      ) THEN 'awaiting_why_read'
                     WHEN NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric IS NULL
                       OR NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric < {threshold_sql}
                       THEN 'below_threshold'
                     WHEN c.why_read IS NULL THEN 'awaiting_why_read'
                     ELSE COALESCE(NULLIF(d.reason, ''), 'below_threshold')
                   END"""
    return f"""WITH window_items AS (
                   SELECT i.id,
                          i.fetched_at,
                          i.title,
                          i.url,
                          i.platform,
                          i.source,
                          i.author_name,
                          i.ai_category,
                          i.cluster_id,
                          i.highlight_scores,
                          i.highlight_uncertainty,
                          i.highlight_reason,
                          i.highlight_verdict,
                          i.highlight_last_error,
                          i.highlight_error_count,
                          i.highlight_retry_after,
                          i.highlight_scored_at,
                          c.ai_title AS cluster_title
                     FROM {schema}.items i
                     LEFT JOIN {schema}.clusters c ON c.id = i.cluster_id
                    WHERE i.fetched_at >= now() - (%(days)s::int * interval '1 day')
                 ),
                 matching_cluster_ids AS (
                   SELECT DISTINCT i.cluster_id
                     FROM window_items i
                    WHERE i.cluster_id IS NOT NULL
                      AND (
                        %(query)s = ''
                        OR i.title ILIKE %(search_pattern)s ESCAPE '\\'
                        OR i.cluster_title ILIKE %(search_pattern)s ESCAPE '\\'
                      )
                 ),
                 selected_window_items AS (
                   SELECT i.*
                     FROM window_items i
                    WHERE %(query)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM matching_cluster_ids matched
                          WHERE matched.cluster_id = i.cluster_id
                       )
                 ),
                 terminal_items AS (
                   SELECT *
                     FROM selected_window_items i
                    WHERE i.highlight_verdict IS NOT NULL
                 ),
                 scored_items AS (
                   SELECT *
                     FROM terminal_items i
                    WHERE i.highlight_verdict <> 'drop'
                 ),
                 clustered_cluster_ids AS (
                   SELECT DISTINCT cluster_id
                     FROM scored_items
                    WHERE cluster_id IS NOT NULL
                 ),
                 clustered_clusters AS (
                   SELECT ids.cluster_id,
                          MAX(i.fetched_at) AS latest_at
                     FROM clustered_cluster_ids ids
                     JOIN selected_window_items i ON i.cluster_id = ids.cluster_id
                    GROUP BY ids.cluster_id
                 ),
                 all_window_cluster_ids AS (
                   SELECT i.cluster_id,
                          MAX(i.fetched_at) AS latest_at,
                          COUNT(*)::int AS member_count,
                          COUNT(*) FILTER (WHERE i.highlight_verdict IS NOT NULL)::int
                            AS terminal_count,
                          COUNT(*) FILTER (WHERE i.highlight_verdict = 'drop')::int
                            AS drop_count,
                          COUNT(*) FILTER (
                            WHERE {actionable_error_filter}
                          )::int AS error_member_count,
                          BOOL_OR(i.highlight_verdict = 'drop') AS has_drop_member
                     FROM selected_window_items i
                    WHERE i.cluster_id IS NOT NULL
                    GROUP BY i.cluster_id
                 ),
                 summarized_clusters AS (
                   SELECT cc.cluster_id,
                          cc.latest_at
                     FROM clustered_clusters cc
                     JOIN {schema}.clusters c ON c.id = cc.cluster_id
                    WHERE c.is_visible_in_feed = true
                      AND c.published_at IS NOT NULL
                      AND COALESCE(c.archived, false) = false
                      AND c.merged_into IS NULL
                      AND NULLIF(BTRIM(c.ai_title), '') IS NOT NULL
                      AND NULLIF(BTRIM(c.ai_summary), '') IS NOT NULL
                      {verdict_filter}
                      {public_filter}
                      {github_filter}
                 ),
                 displayed_clusters AS (
                   SELECT sc.cluster_id,
                          sc.latest_at
                     FROM summarized_clusters sc
                     JOIN {schema}.clusters c ON c.id = sc.cluster_id
                    WHERE true
                      {display_filter}
                 ),
                 category_votes AS (
                   SELECT i.cluster_id,
                          {category_sql} AS category_id,
                          COUNT(*)::int AS votes
                     FROM selected_window_items i
                    WHERE i.cluster_id IS NOT NULL
                    GROUP BY i.cluster_id, {category_sql}
                 ),
                 dominant_categories AS (
                   SELECT DISTINCT ON (cv.cluster_id)
                          cv.cluster_id,
                          cv.category_id
                     FROM category_votes cv
                    WHERE cv.category_id NOT IN ('', 'other')
                    ORDER BY cv.cluster_id, cv.votes DESC, cv.category_id ASC
                 ),
                 panorama_clusters AS (
                   SELECT aw.cluster_id,
                          aw.latest_at,
                          COALESCE(dc.category_id, 'other') AS dominant_category,
                          aw.has_drop_member,
                          CASE
                            WHEN aw.terminal_count = 0 THEN 'pending'
                            WHEN shown.cluster_id IS NOT NULL THEN 'displayed'
                            WHEN summarized.cluster_id IS NOT NULL THEN 'blocked_display'
                            WHEN clustered.cluster_id IS NOT NULL THEN 'blocked_summary'
                            WHEN aw.terminal_count = aw.drop_count
                             AND aw.terminal_count > 0 THEN 'blocked_scoring'
                            ELSE 'blocked_summary'
                          END AS stage,
                          CASE
                            WHEN aw.terminal_count = 0 THEN 'pending_scoring'
                            WHEN shown.cluster_id IS NOT NULL THEN NULL::text
                            WHEN summarized.cluster_id IS NOT NULL THEN {blocked_display_reason}
                            WHEN clustered.cluster_id IS NOT NULL
                              THEN COALESCE(NULLIF(d.reason, ''), 'summary_gate_filtered')
                            WHEN aw.terminal_count = aw.drop_count
                             AND aw.terminal_count > 0 THEN 'all_members_dropped'
                            ELSE COALESCE(NULLIF(d.reason, ''), 'summary_gate_filtered')
                          END AS blocked_reason,
                          (shown.cluster_id IS NOT NULL) AS displayed
                     FROM all_window_cluster_ids aw
                     JOIN {schema}.clusters c ON c.id = aw.cluster_id
                     LEFT JOIN clustered_clusters clustered
                       ON clustered.cluster_id = aw.cluster_id
                     LEFT JOIN summarized_clusters summarized
                       ON summarized.cluster_id = aw.cluster_id
                     LEFT JOIN displayed_clusters shown
                       ON shown.cluster_id = aw.cluster_id
                     LEFT JOIN {schema}.highlight_cluster_decisions d
                       ON d.cluster_id = aw.cluster_id
                     LEFT JOIN dominant_categories dc
                       ON dc.cluster_id = aw.cluster_id
                    WHERE NOT (
                      aw.terminal_count = 0
                      AND aw.error_member_count > 0
                    )
                 ),
                 anomaly_items AS (
                   SELECT i.id,
                          i.fetched_at,
                          i.title,
                          i.url,
                          i.cluster_id,
                          i.highlight_scores,
                          i.highlight_uncertainty,
                          i.highlight_reason,
                          i.highlight_verdict,
                          i.highlight_last_error,
                          i.highlight_error_count,
                          i.highlight_retry_after,
                          i.highlight_scored_at,
                          i.cluster_title,
                          CASE
                            WHEN i.highlight_last_error IS NOT NULL THEN 'scoring'
                            ELSE 'clustering'
                          END AS stuck_at,
                          CASE
                            WHEN i.highlight_last_error IS NOT NULL
                              THEN LEFT(i.highlight_last_error, 500)
                            ELSE 'scored item was not clustered within 30 minutes'
                          END AS error_summary
                     FROM window_items i
                    WHERE (
                      %(query)s = ''
                      OR i.title ILIKE %(search_pattern)s ESCAPE '\\'
                      OR i.cluster_title ILIKE %(search_pattern)s ESCAPE '\\'
                    )
                      AND (
                        (
                          {actionable_error_filter}
                        )
                        OR (
                          i.highlight_verdict <> 'drop'
                          AND i.cluster_id IS NULL
                          AND i.highlight_scored_at <= now() - interval '30 minutes'
                        )
                      )
                 ),
                 funnel_cluster_ids AS (
                   SELECT s.cluster_id
                     FROM panorama_clusters s
                    WHERE {tag_filter}
                 ),
                 funnel_terminal_items AS (
                   SELECT i.*
                     FROM terminal_items i
                    WHERE %(tag)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM funnel_cluster_ids tagged
                          WHERE tagged.cluster_id = i.cluster_id
                       )
                 ),
                 funnel_scored_items AS (
                   SELECT i.*
                     FROM scored_items i
                    WHERE %(tag)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM funnel_cluster_ids tagged
                          WHERE tagged.cluster_id = i.cluster_id
                       )
                 ),
                 funnel_clustered_clusters AS (
                   SELECT c.*
                     FROM clustered_clusters c
                    WHERE %(tag)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM funnel_cluster_ids tagged
                          WHERE tagged.cluster_id = c.cluster_id
                       )
                 ),
                 funnel_summarized_clusters AS (
                   SELECT c.*
                     FROM summarized_clusters c
                    WHERE %(tag)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM funnel_cluster_ids tagged
                          WHERE tagged.cluster_id = c.cluster_id
                       )
                 ),
                 funnel_displayed_clusters AS (
                   SELECT c.*
                     FROM displayed_clusters c
                    WHERE %(tag)s = ''
                       OR EXISTS (
                         SELECT 1
                           FROM funnel_cluster_ids tagged
                          WHERE tagged.cluster_id = c.cluster_id
                       )
                 )"""


def _admin_highlights_funnel_params(
    *,
    days: int,
    q: str | None,
    tag: str | None = "",
) -> dict[str, Any]:
    query, search_pattern = _admin_highlights_funnel_query(q)
    return {
        "days": _admin_highlights_funnel_days(days),
        "query": query,
        "search_pattern": search_pattern,
        "tag": _admin_highlights_funnel_tag(tag),
        "verdict_recent_days": _highlights_verdict_filter_recent_days(),
    }


def query_admin_highlights_funnel_remote(
    *,
    days: int = 1,
    q: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """Return the five audit stations and three exact adjacent-set differences."""
    schema = remote_schema()
    display_threshold = _highlights_display_threshold()
    params = _admin_highlights_funnel_params(days=days, q=q, tag=tag)
    ctes = _admin_highlights_funnel_ctes(
        schema,
        display_threshold=display_threshold,
    )
    with connect() as conn:
        _set_short_statement_timeout(conn, _ADMIN_HIGHLIGHTS_FUNNEL_TIMEOUT_MS)
        row = conn.execute(
            f"""{ctes}
                SELECT (SELECT COUNT(*) FROM funnel_terminal_items) AS ingested_count,
                       (SELECT COUNT(*) FROM funnel_scored_items) AS scored_count,
                       (SELECT COUNT(*) FROM funnel_clustered_clusters) AS clustered_count,
                       (SELECT COUNT(*) FROM funnel_summarized_clusters) AS summarized_count,
                       (SELECT COUNT(*) FROM funnel_displayed_clusters) AS displayed_count,
                       (SELECT COUNT(*) FROM anomaly_items) AS anomalies_count""",
            params,
        ).fetchone()
    values = dict(row or {})
    counts = {
        "ingested": int(values.get("ingested_count") or 0),
        "scored": int(values.get("scored_count") or 0),
        "clustered": int(values.get("clustered_count") or 0),
        "summarized": int(values.get("summarized_count") or 0),
        "displayed": int(values.get("displayed_count") or 0),
    }
    if (
        counts["ingested"] < counts["scored"]
        or counts["clustered"] < counts["summarized"]
        or counts["summarized"] < counts["displayed"]
    ):
        raise RemoteDBError("highlights funnel station invariant violated")
    return {
        "stations": [
            {"key": key, "count": counts[key]}
            for key in ("ingested", "scored", "clustered", "summarized", "displayed")
        ],
        "diffs": [
            {"key": "scoring", "count": counts["ingested"] - counts["scored"]},
            {"key": "summary", "count": counts["clustered"] - counts["summarized"]},
            {"key": "display", "count": counts["summarized"] - counts["displayed"]},
        ],
        "anomalies_count": int(values.get("anomalies_count") or 0),
        "gate_disabled": display_threshold is None,
    }


def _highlight_funnel_dims(scores: Any) -> dict[str, Any]:
    payload = _json_value(scores)
    nested = payload.get("v26") if isinstance(payload, dict) else None
    v26 = nested if isinstance(nested, dict) else {}
    return {
        key: v26.get(key)
        for key in ("authority", "substance", "novelty", "timeliness", "audience_fit")
    }


def _highlight_funnel_score(scores: Any) -> float | None:
    payload = _json_value(scores)
    nested = payload.get("v26") if isinstance(payload, dict) else None
    v26 = nested if isinstance(nested, dict) else {}
    raw_score = v26.get("score10")
    try:
        return float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        return None


def _highlight_funnel_reach(scores: Any) -> int | None:
    payload = _json_value(scores)
    nested = payload.get("v26") if isinstance(payload, dict) else None
    v26 = nested if isinstance(nested, dict) else {}
    raw_reach = v26.get("reach")
    if isinstance(raw_reach, bool):
        return None
    try:
        reach = float(raw_reach)
    except (TypeError, ValueError):
        return None
    return int(reach) if reach in (1.0, 2.0, 3.0) else None






def _admin_highlights_veto(value: Any) -> str | None:
    if value in (None, "", "none"):
        return None
    return str(value)


def _admin_highlights_item_rows(
    *,
    conn: Any,
    schema: str,
    ctes: str,
    params: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    total_row = conn.execute(
        f"""{ctes}
            SELECT COUNT(*) AS total
              FROM anomaly_items s""",
        params,
    ).fetchone()
    rows = conn.execute(
        f"""{ctes}
            SELECT s.id,
                   s.fetched_at AS ingested_at,
                   s.title,
                   s.url,
                   s.cluster_id,
                   s.cluster_title,
                   s.highlight_scores,
                   s.highlight_uncertainty AS uncertainty,
                   COALESCE(NULLIF(s.highlight_reason, ''), s.error_summary) AS reason,
                   s.stuck_at,
                   s.error_summary,
                   fb.action AS feedback_kind,
                   fb.reason AS feedback_note
              FROM anomaly_items s
              LEFT JOIN LATERAL (
                SELECT item_fb.action,
                       item_fb.reason
                  FROM {schema}.item_feedback item_fb
                 WHERE item_fb.item_id = s.id
                   AND item_fb.action IN ('should_feature', 'should_drop')
                 ORDER BY item_fb.created_at DESC, item_fb.id DESC
                 LIMIT 1
              ) fb ON true
             ORDER BY s.fetched_at DESC, s.id DESC
             LIMIT %(limit)s OFFSET %(offset)s""",
        params,
    ).fetchall()
    return int((total_row or {}).get("total") or 0), [dict(row) for row in rows]




def _admin_highlights_panorama_filter() -> str:
    tag_filter = _admin_highlights_tag_filter("s")
    return f"""WHERE (
                     %(display)s = 'all'
                     OR (%(display)s = 'shown' AND s.displayed IS TRUE)
                     OR (%(display)s = 'hidden' AND s.displayed IS FALSE)
                   )
               AND {tag_filter}
               AND (
                     %(stage)s = ''
                     OR (%(stage)s = 'blocked_scoring' AND s.has_drop_member)
                     OR s.stage = %(stage)s
                   )"""


def _admin_highlights_cluster_rows(
    *,
    conn: Any,
    schema: str,
    ctes: str,
    params: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    source_filter = _admin_highlights_panorama_filter()
    total_row = conn.execute(
        f"""{ctes}
            SELECT COUNT(*) AS total
              FROM panorama_clusters s
              {source_filter}""",
        params,
    ).fetchone()
    rows = conn.execute(
        f"""{ctes}
            SELECT c.id,
                   s.latest_at,
                   c.ai_title AS title,
                   s.dominant_category,
                   NULLIF(d.score_inputs->>'max_flag_score10', '')::numeric
                     AS max_flag_score10,
                   d.score_inputs,
                   representative.id AS deciding_item_id,
                   representative.title AS deciding_item_title,
                   representative.highlight_scores AS deciding_item_scores,
                   representative.highlight_reason AS deciding_item_reason,
                   s.stage,
                   s.blocked_reason,
                   s.displayed,
                   d.manual_display,
                   cs.feedback_kind,
                   cs.feedback_note,
                   COALESCE(members.items, '[]'::jsonb) AS members
              FROM panorama_clusters s
              JOIN {schema}.clusters c ON c.id = s.cluster_id
              LEFT JOIN {schema}.highlight_cluster_decisions d ON d.cluster_id = c.id
              LEFT JOIN LATERAL (
                SELECT i_deciding.id,
                       i_deciding.title,
                       i_deciding.highlight_scores,
                       i_deciding.highlight_reason
                  FROM {schema}.items i_deciding
                 WHERE i_deciding.cluster_id = c.id
                   AND i_deciding.fetched_at >= now() - (%(days)s::int * interval '1 day')
                 ORDER BY (i_deciding.id = d.deciding_item_id) DESC,
                          (i_deciding.highlight_scores->'v26'->>'score10')::numeric DESC NULLS LAST,
                          i_deciding.id DESC
                 LIMIT 1
              ) representative ON true
              LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                         jsonb_build_object(
                           'id', i_member.id,
                           'title', i_member.title,
                           'url', i_member.url,
                           'platform', i_member.platform,
                           'source', i_member.source,
                           'author_name', i_member.author_name,
                           'fetched_at', i_member.fetched_at,
                           'verdict', i_member.highlight_verdict,
                           'score10', (i_member.highlight_scores->'v26'->>'score10')::numeric,
                           'highlight_scores', i_member.highlight_scores,
                           'veto', COALESCE(
                             i_member.highlight_scores->'v26'->>'veto',
                             i_member.highlight_scores->>'veto'
                           ),
                           'uncertainty', i_member.highlight_uncertainty,
                           'highlight_reason', i_member.highlight_reason,
                           'feedback_kind', item_fb.action,
                           'feedback_note', item_fb.reason
                         )
                         ORDER BY (i_member.highlight_scores->'v26'->>'score10')::numeric DESC NULLS LAST,
                                  i_member.id DESC
                       ) AS items
                  FROM {schema}.items i_member
                  LEFT JOIN LATERAL (
                    SELECT fb.action,
                           fb.reason
                      FROM {schema}.item_feedback fb
                     WHERE fb.item_id = i_member.id
                       AND fb.action IN ('should_feature', 'should_drop')
                     ORDER BY fb.created_at DESC, fb.id DESC
                     LIMIT 1
                  ) item_fb ON true
                 WHERE i_member.cluster_id = s.cluster_id
                   AND i_member.fetched_at >= now() - (%(days)s::int * interval '1 day')
              ) members ON true
              LEFT JOIN {schema}.cluster_status cs
                ON cs.cluster_id = c.id
               AND cs.user_id = %(user_id)s
              {source_filter}
             ORDER BY s.latest_at DESC, c.id DESC
             LIMIT %(limit)s OFFSET %(offset)s""",
        params,
    ).fetchall()
    return int((total_row or {}).get("total") or 0), [dict(row) for row in rows]


def _admin_highlights_item_payload(row: dict[str, Any], *, anomaly: bool) -> dict[str, Any]:
    scores = row.get("highlight_scores")
    payload = _json_value(scores)
    nested = payload.get("v26") if isinstance(payload, dict) else None
    v26 = nested if isinstance(nested, dict) else {}
    raw_veto = v26.get("veto") or (
        payload.get("veto") if isinstance(payload, dict) else None
    )
    item = {
        "id": str(row.get("id") or ""),
        "ingested_at": to_utc_iso(row.get("ingested_at")),
        "title": row.get("title"),
        "url": row.get("url"),
        "cluster_id": int(row["cluster_id"]) if row.get("cluster_id") is not None else None,
        "cluster_title": row.get("cluster_title"),
        "score10": _highlight_funnel_score(scores),
        "reach": _highlight_funnel_reach(scores),
        "dims": _highlight_funnel_dims(scores),
        "veto": _admin_highlights_veto(raw_veto),
        "uncertainty": row.get("uncertainty"),
        "reason": row.get("reason"),
        "feedback": {
            "kind": row.get("feedback_kind"),
            "note": row.get("feedback_note"),
        },
    }
    if anomaly:
        item["stuck_at"] = row.get("stuck_at")
        item["error_summary"] = row.get("error_summary")
    return item


def _admin_highlights_cluster_payload(row: dict[str, Any]) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    for raw_member in _json_array(row.get("members")):
        member = raw_member if isinstance(raw_member, dict) else {}
        raw_score = member.get("score10")
        try:
            score10 = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score10 = None
        members.append({
            "id": str(member.get("id") or ""),
            "title": member.get("title"),
            "url": member.get("url"),
            "platform": member.get("platform"),
            "source": member.get("source"),
            "author_name": member.get("author_name"),
            "fetched_at": to_utc_iso(member.get("fetched_at")),
            "verdict": member.get("verdict"),
            "score10": score10,
            "reach": _highlight_funnel_reach(member.get("highlight_scores")),
            "dims": _highlight_funnel_dims(member.get("highlight_scores")),
            "veto": _admin_highlights_veto(member.get("veto")),
            "uncertainty": member.get("uncertainty"),
            "reason": member.get("highlight_reason"),
            "feedback": {
                "kind": member.get("feedback_kind"),
                "note": member.get("feedback_note"),
            },
        })
    raw_score = row.get("max_flag_score10")
    stage = str(row.get("stage") or "blocked_summary")
    if stage not in _ADMIN_HIGHLIGHTS_FUNNEL_STAGES - {""}:
        stage = "blocked_summary"
    return {
        "id": int(row["id"]),
        "latest_at": to_utc_iso(row.get("latest_at")),
        "title": row.get("title"),
        "dominant_category": row.get("dominant_category") or "other",
        "max_flag_score10": float(raw_score) if raw_score is not None else None,
        "score_inputs": _json_value(row.get("score_inputs")),
        "deciding_item": {
            "id": str(row.get("deciding_item_id") or ""),
            "title": row.get("deciding_item_title"),
            "dims": _highlight_funnel_dims(row.get("deciding_item_scores")),
            "reason": row.get("deciding_item_reason"),
        },
        "stage": stage,
        "blocked_reason": row.get("blocked_reason"),
        "displayed": bool(row.get("displayed")),
        "manual_display": row.get("manual_display"),
        "feedback": {
            "kind": row.get("feedback_kind"),
            "note": row.get("feedback_note"),
        },
        "members": members,
    }


def query_admin_highlights_funnel_rows_remote(
    *,
    view: str = "panorama",
    days: int = 1,
    q: str = "",
    tag: str = "",
    display: str = "all",
    stage: str = "",
    page: int = 1,
    limit: int = 50,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return the paginated cluster panorama or anomaly fallback view."""
    safe_view = str(view or "").strip()
    if safe_view not in _ADMIN_HIGHLIGHTS_FUNNEL_VIEWS:
        raise ValueError("unsupported funnel view")
    safe_display = str(display or "all").strip()
    if safe_display not in _ADMIN_HIGHLIGHTS_FUNNEL_DISPLAYS:
        raise ValueError("display must be all, shown, or hidden")
    safe_stage = str(stage or "").strip()
    if safe_stage not in _ADMIN_HIGHLIGHTS_FUNNEL_STAGES:
        raise ValueError("unsupported funnel stage")
    safe_tag = _admin_highlights_funnel_tag(tag)
    safe_page = max(1, int(page or 1))
    safe_limit = max(1, min(int(limit or 50), 100))
    schema = remote_schema()
    display_threshold = _highlights_display_threshold()
    params = _admin_highlights_funnel_params(days=days, q=q, tag=safe_tag)
    params.update({
        "user_id": str(user_id) if user_id is not None else None,
        "tag": safe_tag,
        "display": safe_display,
        "stage": safe_stage,
        "limit": safe_limit,
        "offset": (safe_page - 1) * safe_limit,
    })
    ctes = _admin_highlights_funnel_ctes(
        schema,
        display_threshold=display_threshold,
    )
    item_view = safe_view == "anomaly"
    with connect() as conn:
        _set_short_statement_timeout(conn, _ADMIN_HIGHLIGHTS_FUNNEL_TIMEOUT_MS)
        if item_view:
            total, rows = _admin_highlights_item_rows(
                conn=conn,
                schema=schema,
                ctes=ctes,
                params=params,
            )
        else:
            total, rows = _admin_highlights_cluster_rows(
                conn=conn,
                schema=schema,
                ctes=ctes,
                params=params,
            )
    items = (
        [
            _admin_highlights_item_payload(row, anomaly=safe_view == "anomaly")
            for row in rows
        ]
        if item_view
        else [_admin_highlights_cluster_payload(row) for row in rows]
    )
    return {
        "granularity": "item" if item_view else "cluster",
        "items": items,
        "total": total,
        "page": safe_page,
        "gate_disabled": display_threshold is None,
        "display_threshold": display_threshold,
    }


def query_pending_highlight_verdict_items_remote(
    *,
    limit: int | None = None,
    ids: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    require_published_at: bool = False,
    rescore_prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    """Return Supabase items that still need item-level Highlights verdicts."""
    schema = remote_schema()
    select_cols = """id, platform, source, author_name, metrics_json, url, title, content,
                     ai_summary, ai_category as category, detail_json, asr_text"""
    params: list[Any] = []
    clauses = ["platform <> 'bilibili'"]
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        clauses = [f"id IN ({placeholders})"]
        params.extend(ids)
    else:
        time_expr = (
            "published_at"
            if require_published_at
            else "COALESCE(published_at, fetched_at)"
        )
        if require_published_at:
            clauses.append("published_at IS NOT NULL")
        if window_start:
            clauses.append(f"{time_expr} >= %s")
            params.append(_timestamp_value(window_start))
        if window_end:
            clauses.append(f"{time_expr} < %s")
            params.append(_timestamp_value(window_end))
        clauses.extend(
            [
                "ai_summary IS NOT NULL",
                "(highlight_retry_after IS NULL OR highlight_retry_after <= now())",
            ]
        )
        if rescore_prompt_version:
            clauses.append("(highlight_verdict IS NULL OR highlight_prompt_version IS DISTINCT FROM %s)")
            params.append(str(rescore_prompt_version))
        else:
            clauses.append("(highlight_verdict IS NULL)")
    order_expr = "COALESCE(published_at, fetched_at)" if (window_start or window_end) else "fetched_at"
    limit_clause = ""
    if limit:
        limit_clause = " LIMIT %s"
        params.append(limit)
    with connect() as conn:
        set_pending_scan_statement_timeout(conn)
        rows = conn.execute(
            f"""SELECT {select_cols}
                  FROM {schema}.items
                 WHERE {' AND '.join(clauses)}
                 ORDER BY {order_expr} DESC{limit_clause}""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def write_highlight_verdict_remote(pg_conn: Any | None, item_id: str, result: dict[str, Any]) -> None:
    """Write item-level Highlights verdict metadata to Supabase."""
    if pg_conn is None:
        with connect() as conn:
            write_highlight_verdict_remote(conn, item_id, result)
            return

    pending = result.get("cluster_verdict") == "pending" or not result.get("highlight_verdict")
    retry_after_value = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
        if pending
        else None
    )
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET highlight_verdict = %s,
                   highlight_value_path = %s,
                   highlight_uncertainty = %s,
                   highlight_include_in_highlights = %s,
                   highlight_reason = %s,
                   highlight_scores = %s,
                   highlight_ai_relevant = %s,
                   highlight_spam = %s,
                   highlight_confidence = %s,
                   highlight_prompt_version = %s,
                   highlight_model = %s,
                   highlight_scored_at = COALESCE(%s::timestamptz, now()),
                   highlight_error_count = CASE
                     WHEN %s THEN COALESCE(highlight_error_count, 0) + 1
                     ELSE 0
                   END,
                   highlight_last_error = %s,
                   highlight_retry_after = %s
             WHERE id = %s""",
        (
            result.get("highlight_verdict"),
            result.get("highlight_value_path"),
            result.get("highlight_uncertainty"),
            bool(result.get("highlight_include_in_highlights")),
            result.get("highlight_reason"),
            _maybe_jsonb(result.get("highlight_scores") or {}),
            result.get("highlight_ai_relevant"),
            result.get("highlight_spam"),
            result.get("highlight_confidence"),
            result.get("highlight_prompt_version"),
            result.get("highlight_model"),
            _timestamp_value(result.get("highlight_scored_at")),
            pending,
            result.get("highlight_last_error"),
            retry_after_value,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def write_highlight_score_v26_remote(
    pg_conn: Any | None,
    item_id: str,
    result: dict[str, Any],
    *,
    threshold: float,
) -> None:
    """Merge a v26 item score into nested highlight_scores metadata."""
    if pg_conn is None:
        with connect() as conn:
            write_highlight_score_v26_remote(
                conn,
                item_id,
                result,
                threshold=threshold,
            )
            return

    dims = result.get("dims") or {}
    v26_score = {
        "authority": dims.get("authority"),
        "substance": dims.get("substance"),
        "novelty": dims.get("novelty"),
        "timeliness": dims.get("timeliness"),
        "audience_fit": dims.get("audience_fit"),
        "marketing": result.get("marketing"),
        "score10": result.get("score10"),
        "content_type": result.get("content_type"),
        "reject": result.get("reject"),
        "veto": result.get("veto"),
        "reach": result.get("reach"),
    }
    for key in ("runs", "pass2_error"):
        if key in result:
            v26_score[key] = result.get(key)
    if result.get("reject") or result.get("veto") != "none":
        verdict = "drop"
    elif result.get("score10") is not None and result["score10"] >= threshold:
        verdict = "featured"
    else:
        verdict = "borderline"
    include_in_highlights = bool(result.get("is_flag_bearer"))
    reason = result.get("reason")
    if (
        verdict == "featured"
        and result.get("reach") == 1
        and result.get("value_path") != "major_event"
    ):
        include_in_highlights = False
        verdict = "drop"
        reason = str(reason or "")
        if not reason.startswith("[reach_guard] "):
            reason = f"[reach_guard] {reason}"

    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET highlight_scores = COALESCE(highlight_scores, '{{}}'::jsonb)
                                      || jsonb_build_object('v26', %s::jsonb),
                   highlight_include_in_highlights = %s,
                   highlight_verdict = %s,
                   highlight_value_path = %s,
                   highlight_uncertainty = %s,
                   highlight_reason = %s,
                   highlight_confidence = %s,
                   highlight_prompt_version = %s,
                   highlight_scored_at = now(),
                   highlight_error_count = 0,
                   highlight_last_error = NULL,
                   highlight_retry_after = NULL
             WHERE id = %s""",
        (
            _maybe_jsonb(v26_score),
            include_in_highlights,
            verdict,
            result.get("value_path"),
            result.get("uncertainty"),
            reason,
            result.get("confidence"),
            highlight_score_v26.PROMPT_VERSION,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def record_highlight_verdict_failure_remote(
    pg_conn: Any | None,
    item_id: str,
    error: str,
    *,
    retry_after: Any = None,
) -> None:
    """Record highlight-verdict failure metadata without touching enrichment fields."""
    if pg_conn is None:
        with connect() as conn:
            record_highlight_verdict_failure_remote(
                conn,
                item_id,
                error,
                retry_after=retry_after,
            )
            return

    retry_after_value = None
    if isinstance(retry_after, (int, float)):
        retry_after_value = datetime.now(timezone.utc).timestamp() + float(retry_after)
        retry_after_value = datetime.fromtimestamp(retry_after_value, tz=timezone.utc)
    elif retry_after:
        retry_after_value = _timestamp_value(retry_after)
    pg_conn.execute(
        f"""UPDATE {remote_schema()}.items
               SET highlight_error_count = COALESCE(highlight_error_count, 0) + 1,
                   highlight_last_error = %s,
                   highlight_retry_after = %s,
                   highlight_scored_at = now()
             WHERE id = %s""",
        (
            str(error or "")[:1000],
            retry_after_value,
            item_id,
        ),
    )
    _commit_if_supported(pg_conn)


def set_admin_highlight_cluster_override_remote(
    *,
    cluster_id: int,
    user_id: str,
    action: str,
    note: str | None = None,
) -> dict[str, Any] | None:
    """Set/clear display override and its admin label in one transaction."""
    if action not in {"force_show", "force_hide", "clear"}:
        raise ValueError("invalid override action")
    schema = remote_schema()
    with connect() as conn:
        exists = conn.execute(
            f"SELECT 1 FROM {schema}.clusters WHERE id = %(cluster_id)s",
            {"cluster_id": cluster_id},
        ).fetchone()
        if not exists:
            return None

        if action == "clear":
            row = conn.execute(
                f"""UPDATE {schema}.highlight_cluster_decisions
                       SET manual_display = NULL,
                           manual_display_at = NULL
                     WHERE cluster_id = %(cluster_id)s
                 RETURNING manual_display, manual_display_at""",
                {"cluster_id": cluster_id},
            ).fetchone()
            conn.execute(
                f"""UPDATE {schema}.cluster_status
                       SET feedback_kind = NULL,
                           feedback_at = NULL,
                           feedback_note = NULL
                     WHERE user_id = %(user_id)s
                       AND cluster_id = %(cluster_id)s""",
                {"user_id": user_id, "cluster_id": cluster_id},
            )
            feedback_kind = None
            feedback_note = None
        else:
            feedback_kind = "should_feature" if action == "force_show" else "irrelevant"
            row = conn.execute(
                f"""INSERT INTO {schema}.highlight_cluster_decisions AS target (
                         cluster_id, decision, cluster_verdict, verdict_counts_json,
                         snapshot_json, manual_display, manual_display_at
                       )
                       SELECT id, 'pending', 'pending', '{{}}'::jsonb,
                              '{{}}'::jsonb, %(action)s, now()
                         FROM {schema}.clusters
                        WHERE id = %(cluster_id)s
                       ON CONFLICT (cluster_id) DO UPDATE SET
                         manual_display = excluded.manual_display,
                         manual_display_at = CASE
                           WHEN target.manual_display IS DISTINCT FROM excluded.manual_display
                             THEN excluded.manual_display_at
                           ELSE target.manual_display_at
                         END
                   RETURNING manual_display, manual_display_at""",
                {"cluster_id": cluster_id, "action": action},
            ).fetchone()
            conn.execute(
                f"""INSERT INTO {schema}.cluster_status (
                         user_id, cluster_id, feedback_kind, feedback_at, feedback_note
                       )
                       VALUES (
                         %(user_id)s, %(cluster_id)s, %(feedback_kind)s, now(), %(note)s
                       )
                       ON CONFLICT (user_id, cluster_id) DO UPDATE SET
                         feedback_kind = excluded.feedback_kind,
                         feedback_at = excluded.feedback_at,
                         feedback_note = excluded.feedback_note""",
                {
                    "user_id": user_id,
                    "cluster_id": cluster_id,
                    "feedback_kind": feedback_kind,
                    "note": note,
                },
            )
            feedback_note = note
        conn.execute(
            f"""UPDATE {schema}.clusters
                   SET last_updated_at = now()
                 WHERE id = %(cluster_id)s""",
            {"cluster_id": cluster_id},
        )
        conn.commit()

    clear_feed_cache_keys()
    clear_user_cache_keys(user_id)
    return {
        "ok": True,
        "manual_display": (row or {}).get("manual_display"),
        "manual_display_at": to_utc_iso((row or {}).get("manual_display_at")),
        "feedback_kind": feedback_kind,
        "feedback_note": feedback_note,
        "data_backend": status_backend(),
    }


def query_highlight_cluster_decisions_remote(
    *,
    decision: str = "excluded",
    cluster_verdict: str | None = None,
    recent_days: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return machine highlight decisions for review tooling."""
    safe_decision = decision if decision in {"included", "excluded", "pending"} else "excluded"
    safe_cluster_verdict = (
        cluster_verdict
        if cluster_verdict in {"featured", "positive_borderline", "risk_borderline", "drop", "pending"}
        else None
    )
    try:
        safe_recent_days = max(0, min(int(recent_days or 0), 365))
    except (TypeError, ValueError):
        safe_recent_days = 0
    safe_limit = max(1, min(int(limit or 100), 500))
    schema = remote_schema()
    where = ["d.decision = %s"]
    params: list[Any] = [safe_decision]
    if safe_cluster_verdict:
        where.append("d.cluster_verdict = %s")
        params.append(safe_cluster_verdict)
    if safe_recent_days:
        where.append(
            "COALESCE(c.last_doc_at, c.first_doc_at, c.last_updated_at, d.decided_at) "
            ">= now() - (%s::int * interval '1 day')"
        )
        params.append(safe_recent_days)
    params.append(safe_limit)
    with connect() as conn:
        rows = conn.execute(
            f"""WITH filtered_decisions AS (
                   SELECT d.cluster_id,
                          d.decision,
                          d.cluster_verdict,
                          d.deciding_item_id,
                          d.reason,
                          d.verdict_counts_json,
                          d.prompt_version,
                          d.model,
                          d.decided_at,
                          d.updated_at,
                          d.snapshot_json,
                          c.ai_title,
                          c.ai_summary,
                          c.doc_count,
                          c.unique_source_count,
                          c.first_doc_at,
                          c.last_doc_at
                     FROM {schema}.highlight_cluster_decisions d
                     LEFT JOIN {schema}.clusters c ON c.id = d.cluster_id
                    WHERE {" AND ".join(where)}
                    ORDER BY d.decided_at DESC, d.cluster_id DESC
                    LIMIT %s
                 )
                SELECT fd.cluster_id,
                       fd.decision,
                       fd.cluster_verdict,
                       fd.deciding_item_id,
                       fd.reason,
                       fd.verdict_counts_json,
                       fd.prompt_version,
                       fd.model,
                       fd.decided_at,
                       fd.updated_at,
                       fd.snapshot_json,
                       fd.ai_title,
                       fd.ai_summary,
                       fd.doc_count,
                       fd.unique_source_count,
                       fd.first_doc_at,
                       fd.last_doc_at,
                       lr.human_verdict AS latest_human_verdict,
                       lr.error_kind AS latest_error_kind,
                       lr.notes AS latest_notes,
                       lr.reviewer AS latest_reviewer,
                       lr.reviewed_at AS latest_reviewed_at
                  FROM filtered_decisions fd
                  LEFT JOIN LATERAL (
                       SELECT r.human_verdict,
                              r.error_kind,
                              r.notes,
                              r.reviewer,
                              r.reviewed_at
                         FROM {schema}.highlight_exclusion_reviews r
                        WHERE r.cluster_id = fd.cluster_id
                        ORDER BY r.reviewed_at DESC, r.id DESC
                        LIMIT 1
                  ) lr ON true
                 ORDER BY fd.decided_at DESC, fd.cluster_id DESC""",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def query_highlight_review_docs_remote(cluster_id: int) -> list[dict[str, Any]]:
    """Return source docs/items for a cluster highlight review page."""
    schema = remote_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.id,
                       i.title,
                       i.url,
                       i.platform,
                       i.source,
                       i.author_name,
                       i.ai_summary,
                       i.content,
                       i.published_at,
                       i.fetched_at,
                       ci.rank_in_cluster,
                       COALESCE(ci.is_primary_source, false) AS is_primary_source,
                       i.highlight_verdict,
                       i.highlight_value_path,
                       i.highlight_uncertainty,
                       i.highlight_include_in_highlights,
                       i.highlight_reason
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = %s
                 ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                          ci.rank_in_cluster ASC NULLS LAST,
                          COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                          i.id DESC""",
            (int(cluster_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def write_highlight_exclusion_review_remote(
    pg_conn: Any | None,
    *,
    cluster_id: int,
    human_verdict: str,
    machine_decision_at: Any = None,
    error_kind: str | None = None,
    notes: str | None = None,
    reviewer: str | None = None,
) -> None:
    """Append one human review entry for an excluded/pending highlight cluster."""
    if human_verdict not in {"should_feature", "confirmed_drop", "unsure"}:
        raise RemoteDBConfigError(f"Unsupported human_verdict={human_verdict!r}")
    if pg_conn is None:
        with connect() as conn:
            write_highlight_exclusion_review_remote(
                conn,
                cluster_id=cluster_id,
                human_verdict=human_verdict,
                machine_decision_at=machine_decision_at,
                error_kind=error_kind,
                notes=notes,
                reviewer=reviewer,
            )
            return
    pg_conn.execute(
        f"""INSERT INTO {remote_schema()}.highlight_exclusion_reviews (
               cluster_id, machine_decision_at, human_verdict,
               error_kind, notes, reviewer, reviewed_at
             )
             VALUES (%s, %s, %s, %s, %s, %s, now())""",
        (
            int(cluster_id),
            _timestamp_value(machine_decision_at),
            human_verdict,
            str(error_kind or "").strip()[:120] or None,
            str(notes or "").strip()[:1000] or None,
            str(reviewer or "").strip()[:120] or None,
        ),
    )
    _commit_if_supported(pg_conn)


def toggle_item_admin_feedback_remote(
    *,
    item_id: str,
    action: str,
    platform: str | None = None,
    title: str | None = None,
    author: str | None = None,
    url: str | None = None,
    reason: str | None = None,
    topic: str | None = None,
) -> bool:
    """Toggle one admin item label in both remote copies in one transaction."""
    if action not in {"should_feature", "should_drop"}:
        raise ValueError("unsupported admin item feedback action")
    schema = remote_schema()
    with connect() as conn:
        existing = conn.execute(
            f"""SELECT 1 AS exists
                  FROM {schema}.feedback
                 WHERE item_id = %s
                   AND type = %s
                 LIMIT 1""",
            (item_id, action),
        ).fetchone()
        active = existing is None
        conn.execute(
            f"""DELETE FROM {schema}.feedback
                 WHERE item_id = %s
                   AND type IN ('should_feature', 'should_drop')""",
            (item_id,),
        )
        conn.execute(
            f"""DELETE FROM {schema}.item_feedback
                 WHERE item_id = %s
                   AND action IN ('should_feature', 'should_drop')""",
            (item_id,),
        )
        if active:
            conn.execute(
                f"""INSERT INTO {schema}.feedback (item_id, type, topic, text)
                    VALUES (%s, %s, %s, %s)""",
                (item_id, action, topic, reason),
            )
            conn.execute(
                f"""INSERT INTO {schema}.item_feedback
                      (item_id, platform, item_title, item_author, item_url,
                       action, reason, topic_at_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (item_id, platform, title, author, url, action, reason, topic),
            )
        conn.commit()
    return active


def toggle_item_should_feature_remote(
    *,
    item_id: str,
    platform: str | None = None,
    title: str | None = None,
    author: str | None = None,
    url: str | None = None,
    reason: str | None = None,
    topic: str | None = None,
) -> bool:
    return toggle_item_admin_feedback_remote(
        item_id=item_id,
        action="should_feature",
        platform=platform,
        title=title,
        author=author,
        url=url,
        reason=reason,
        topic=topic,
    )


def _highlights_scope_key(*, dimension: str, value: str | None = None) -> str:
    if dimension == "all":
        return "all"
    return f"{dimension}:{value or ''}"


def _highlights_scope_for_categories(categories: list[str] | None) -> str | None:
    if not categories:
        return _highlights_scope_key(dimension="all")
    normalized: set[str] = set()
    for category in categories:
        cid = _category_l1(category)
        if not cid:
            return None
        normalized.add(cid)
    if len(normalized) != 1:
        return None
    return _highlights_scope_key(dimension="category", value=next(iter(normalized)))


def _normalize_highlights_read_model_cursor(
    cursor: Any,
    *,
    expected_scope_key: str,
    expected_version_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(cursor, dict):
        return None
    version_id = str(cursor.get("version_id") or "").strip()
    scope_key = str(cursor.get("scope_key") or "").strip()
    try:
        normalized_version_id = str(uuid.UUID(version_id))
    except (TypeError, ValueError):
        return None
    if expected_version_id and normalized_version_id != expected_version_id:
        return None
    if scope_key != expected_scope_key:
        return None
    try:
        rank_after = int(cursor.get("rank_after"))
    except (TypeError, ValueError):
        return None
    if rank_after < 0:
        return None
    return {
        "version_id": normalized_version_id,
        "scope_key": scope_key,
        "rank_after": rank_after,
    }


def _event_from_highlights_card(
    value: Any,
    *,
    user_last_seen: dict[int, int | None] | None = None,
) -> dict[str, Any] | None:
    card = _json_value(value)
    if not isinstance(card, dict):
        return None
    try:
        cluster_id = int(card.get("id"))
    except (TypeError, ValueError):
        return None
    seen_map = user_last_seen or {}
    live_version = int(card.get("live_version") or 0)
    seen = seen_map.get(cluster_id)
    return {
        "id": cluster_id,
        "ai_title": card.get("ai_title"),
        "ai_summary": card.get("ai_summary"),
        "why_read": card.get("why_read"),
        "doc_count": int(card.get("doc_count") or 0),
        "unique_source_count": int(card.get("unique_source_count") or 0),
        "category": card.get("category"),
        "source_preview": _json_array(card.get("source_preview")),
        "first_doc_at": to_utc_iso(card.get("first_doc_at")) or card.get("first_doc_at"),
        "last_doc_at": to_utc_iso(card.get("last_doc_at")) if card.get("last_doc_at") else None,
        "platforms": _json_array(card.get("platforms")),
        "cover_url": card.get("cover_url"),
        "has_update": bool(seen is not None and live_version > seen),
        "live_version": live_version,
        "last_seen_version": seen,
    }


def _events_date_seek_cte(ordered_rows_sql: str) -> str:
    """Resolve the anchor and its ordinary page within the page query snapshot."""
    return f"""WITH date_seek_rows AS MATERIALIZED ({ordered_rows_sql}),
                   date_seek_anchor AS (
                     SELECT cluster_id AS anchor_event_id,
                            position / %(seek_limit)s * %(seek_limit)s AS page_offset
                       FROM date_seek_rows
                      WHERE sort_at >= %(target_day_start)s::timestamptz
                        AND sort_at < %(target_day_end)s::timestamptz
                      ORDER BY position
                      LIMIT 1
                   ) """


def _events_date_seek_params(target_date: str, timezone_offset_minutes: int, limit: int) -> dict[str, Any]:
    start = datetime.fromisoformat(target_date).replace(tzinfo=timezone.utc) + timedelta(minutes=timezone_offset_minutes)
    return {"target_day_start": start, "target_day_end": start + timedelta(days=1), "seek_limit": limit}


def _events_date_seek_result(target_date: str, rows: list[Any]) -> dict[str, Any]:
    anchor_id = int(rows[0]["anchor_event_id"]) if rows else None
    return {"requested_date": target_date, "status": "found" if anchor_id is not None else "not_found", "anchor_event_id": anchor_id}


def _query_highlights_read_model_events(
    *,
    conn: Any,
    schema: str,
    page: int,
    limit: int,
    cursor: dict[str, Any] | None,
    since_version_snapshot: int | None,
    fetched_since: str | None,
    user_id: str | None,
    public_only: bool,
    min_github_stars: int,
    enabled: bool,
    categories: list[str] | None,
    timezone_offset_minutes: int,
    display_threshold: float | None = None,
    target_date: str | None = None,
) -> dict[str, Any] | None:
    if not _highlights_read_model_enabled():
        return None
    if fetched_since or since_version_snapshot is not None:
        return None
    if not public_only and not user_id:
        return None
    if int(min_github_stars) != HIGHLIGHTS_READ_MODEL_MIN_GITHUB_STARS:
        return None
    scope_key = _highlights_scope_for_categories(categories)
    if not scope_key:
        return None
    display_join = f"LEFT JOIN {schema}.clusters c ON c.id = h.cluster_id"
    display_filter = _highlights_display_cluster_filter(
        schema,
        "c",
        threshold=display_threshold,
    )
    safe_limit = max(1, min(int(limit), 100))
    safe_page = max(1, int(page or 1))
    published_before = highlights_published_before()
    try:
        if not _set_events_read_model_timeouts(conn):
            return None
        cursor_state = _normalize_highlights_read_model_cursor(
            cursor,
            expected_scope_key=scope_key,
        )
        active_data = None
        if cursor_state:
            pinned = conn.execute(
                f"""SELECT v.version_id::text AS version_id,
                           sc.scope_key,
                           sc.total_count,
                           sc.max_sort_at,
                           sc.generated_at
                      FROM {schema}.highlights_read_model_versions v
                      JOIN {schema}.highlights_scopes sc
                        ON sc.version_id = v.version_id
                     WHERE v.version_id = %(version_id)s::uuid
                       AND v.status = 'complete'
                       AND sc.scope_key = %(scope_key)s""",
                {
                    "version_id": cursor_state["version_id"],
                    "scope_key": scope_key,
                },
            ).fetchone()
            if pinned:
                active_data = dict(pinned)
        if active_data is None:
            active = conn.execute(
                f"""SELECT v.version_id::text AS version_id,
                           sc.scope_key,
                           sc.total_count,
                           sc.max_sort_at,
                           sc.generated_at
                      FROM {schema}.highlights_read_model_state st
                      JOIN {schema}.highlights_read_model_versions v
                        ON v.version_id = st.active_version_id
                      JOIN {schema}.highlights_scopes sc
                        ON sc.version_id = v.version_id
                     WHERE st.key = %(state_key)s
                       AND v.status = 'complete'
                       AND sc.scope_key = %(scope_key)s""",
                {
                    "state_key": HIGHLIGHTS_READ_MODEL_STATE_KEY,
                    "scope_key": scope_key,
                },
            ).fetchone()
            if not active:
                return None
            active_data = dict(active)
            cursor_state = None
        version_id = str(active_data.get("version_id") or "")
        rank_after = int(cursor_state["rank_after"]) if cursor_state else (safe_page - 1) * safe_limit
        seek_cte = ""
        seek_columns = ""
        seek_join = ""
        page_offset_sql = "%(rank_after)s"
        seek_params = {}
        if target_date:
            seek_cte = _events_date_seek_cte(f"""
                SELECT h.cluster_id, h.sort_at,
                       row_number() OVER (ORDER BY {_highlights_scope_item_order_sql("h")}) - 1 AS position
                  FROM {schema}.highlights_scope_items h
                  {display_join}
                 WHERE h.version_id = %(version_id)s::uuid
                   AND h.scope_key = %(scope_key)s
                   AND h.sort_at < %(published_before)s
                   {display_filter}
            """)
            seek_columns = ", date_seek_anchor.anchor_event_id, date_seek_anchor.page_offset"
            seek_join = "CROSS JOIN date_seek_anchor"
            page_offset_sql = "COALESCE((SELECT page_offset FROM date_seek_anchor), 0)"
            seek_params = _events_date_seek_params(target_date, timezone_offset_minutes, safe_limit)
        rows = conn.execute(
            f"""{seek_cte}SELECT h.rank, h.cluster_id, h.sort_at, h.card_json,
                       c.why_read AS why_read,
                       d.highlight_score AS highlight_score,
                       (d.score_inputs->>'max_flag_score10')::float AS max_flag_score10,
                       d.cluster_verdict AS cluster_verdict,
                       (d.verdict_counts_json->>'featured')::int AS featured_count,
                       di.highlight_value_path AS value_path {seek_columns}
                  FROM {schema}.highlights_scope_items h
                  {display_join}
                  {seek_join}
                  LEFT JOIN {schema}.highlight_cluster_decisions d
                    ON d.cluster_id = h.cluster_id
                  LEFT JOIN {schema}.items di
                    ON di.id = d.deciding_item_id
                 WHERE h.version_id = %(version_id)s::uuid
                   AND h.scope_key = %(scope_key)s
                   AND h.sort_at < %(published_before)s
                   {display_filter}
                 ORDER BY {_highlights_scope_item_order_sql("h")}
                 OFFSET {page_offset_sql}
                 LIMIT %(limit_plus_one)s""",
            {
                "version_id": version_id,
                "scope_key": scope_key,
                "rank_after": rank_after,
                "limit_plus_one": safe_limit + 1,
                **seek_params,
                "published_before": published_before,
            },
        ).fetchall()
        date_counts_cache_key = (
            "events_highlights_date_counts_v1",
            published_before,
            schema,
            version_id,
            scope_key,
            int(timezone_offset_minutes),
            int(active_data.get("total_count") or 0),
            _timestamp_value(active_data.get("max_sort_at")),
            _timestamp_value(active_data.get("generated_at")),
            display_threshold,
        )
        date_counts = _cache_get_copy(date_counts_cache_key)
        if date_counts is None:
            date_count_rows = conn.execute(
                f"""SELECT COALESCE(
                              to_char((
                                h.sort_at - (%(timezone_offset_minutes)s::int * interval '1 minute')
                              )::date, 'YYYY-MM-DD'),
                              'unknown'
                            ) AS day,
                           count(*) AS n
                      FROM {schema}.highlights_scope_items h
                      {display_join}
                     WHERE h.version_id = %(version_id)s::uuid
                       AND h.scope_key = %(scope_key)s
                       AND h.sort_at < %(published_before)s
                       {display_filter}
                     GROUP BY day""",
                {
                    "version_id": version_id,
                    "scope_key": scope_key,
                    "timezone_offset_minutes": timezone_offset_minutes,
                    "published_before": published_before,
                },
            ).fetchall()
            date_counts = {
                str(row["day"] or "unknown"): int(row["n"] or 0)
                for row in date_count_rows
            }
            _cache_set_copy(date_counts_cache_key, date_counts)
        has_more = len(rows) > safe_limit
        if target_date and rows:
            rank_after = int(rows[0]["page_offset"])
        page_rows = [dict(row) for row in rows[:safe_limit]]
        # P0-2: seen 状态不在内容层查询,由 fetch_events 的 overlay 覆盖,
        # read model 结果因此可被所有登录用户共享缓存。
        seen_map: dict[int, int | None] = {}
        _commit_safely(conn)
        events: list[dict[str, Any]] = []
        for row in page_rows:
            event = _event_from_highlights_card(row.get("card_json"), user_last_seen=seen_map)
            if event:
                # v25.0 F-B：decisions 侧质量字段（LEFT JOIN 缺行时全为 null，前端 fallback）
                score = row.get("highlight_score")
                featured = row.get("featured_count")
                event["highlight_score"] = float(score) if score is not None else None
                event["display_score"] = _display_score_from_max_flag_score10(
                    row.get("max_flag_score10")
                )
                event["why_read"] = row.get("why_read")
                event["cluster_verdict"] = row.get("cluster_verdict")
                event["value_path"] = row.get("value_path")
                event["featured_count"] = int(featured) if featured is not None else None
                events.append(event)
        if target_date and rows and not any(event["id"] == int(rows[0]["anchor_event_id"]) for event in events):
            # A malformed card cannot provide a client anchor; let the caller
            # resolve the same date through its existing live-query fallback.
            return None
        next_cursor = None
        if has_more and page_rows:
            next_cursor = {
                "version_id": version_id,
                "scope_key": scope_key,
                "rank_after": rank_after + len(page_rows),
            }
        result = {
            "enabled": enabled,
            "events": events,
            "next_cursor": next_cursor,
            "new_since_last_fetch": 0,
            "total_available_within_30d": sum(int(count or 0) for count in date_counts.values()),
            "date_counts": date_counts,
            "data_backend": event_read_backend(),
            "read_model": HIGHLIGHTS_READ_MODEL_VERSION,
            "read_model_version_id": version_id,
            "scope_key": scope_key,
        }
        if target_date:
            result["date_seek"] = _events_date_seek_result(target_date, rows)
        return result
    except Exception:
        _rollback_safely(conn)
        return None
