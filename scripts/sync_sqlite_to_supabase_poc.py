#!/usr/bin/env python3
"""Sync a bounded SQLite sample into Supabase Postgres for Phase 0 POC.

This script writes only to the `remote_poc` schema by default. It is not the
production database adapter and intentionally keeps the current SQLite app path
untouched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from export_remote_db_sample import ordered_unique, placeholders, sample_ids


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "feed.db"
DEFAULT_MIGRATION = ROOT / "supabase" / "migrations" / "0001_remote_db_poc.sql"
VECTOR_DIM = 1536
VECTOR_SQL_TYPE = "extensions.vector"
SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
SYNC_TABLES = (
    "items",
    "clusters",
    "cluster_items",
    "item_status",
    "cluster_status",
    "fetch_runs",
    "cluster_judge_log",
)
HEAVY_ITEM_FIELDS = ("content", "detail_json", "comments_json")
ITEM_COLUMNS = (
    "id",
    "user_id",
    "platform",
    "source",
    "fetch_run_id",
    "title",
    "content",
    "author_name",
    "author_id",
    "author_avatar",
    "url",
    "cover_url",
    "description",
    "media_json",
    "metrics_json",
    "tags_json",
    "lang",
    "detail_json",
    "comments_json",
    "ai_summary",
    "ai_key_points",
    "ai_category",
    "ai_keywords",
    "ai_categories",
    "ai_subcategories",
    "multi_l1_reason",
    "ai_extracted",
    "content_type",
    "ai_quality_score",
    "visible",
    "relevance_score",
    "embedding",
    "embedding_provider",
    "embedding_model",
    "embedding_input_variant",
    "embedding_generated_at",
    "canonical_url",
    "cluster_id",
    "fetched_at",
    "published_at",
    "created_at",
)
CLUSTER_COLUMNS = (
    "id",
    "ai_title",
    "ai_summary",
    "ai_key_points",
    "live_version",
    "doc_count",
    "unique_source_count",
    "platforms_json",
    "cover_url",
    "first_doc_at",
    "last_doc_at",
    "last_updated_at",
    "is_visible_in_feed",
    "merged_into",
    "archived",
    "prompt_version",
    "representative_vector",
    "event_embedding",
    "created_run_id",
    "last_touched_run_id",
    "published_run_id",
    "published_at",
    "created_at",
)
CLUSTER_ITEM_COLUMNS = (
    "cluster_id",
    "item_id",
    "rank_in_cluster",
    "added_at",
    "is_primary_source",
    "source_identity",
    "join_decision_id",
)
ITEM_STATUS_COLUMNS = ("user_id", "item_id", "read_at", "clicked_at", "starred_at", "hidden_at")
CLUSTER_STATUS_COLUMNS = ("user_id", "cluster_id", "clicked_at", "starred_at", "last_seen_version")
FETCH_RUN_COLUMNS = ("id", "started_at", "finished_at", "status", "stats_json", "error_msg")
JUDGE_LOG_COLUMNS = (
    "id",
    "item_id",
    "candidate_cluster_ids",
    "llm_input_tokens",
    "llm_output_tokens",
    "matches_json",
    "selected_cluster_id",
    "selection_reason",
    "possible_merge_candidates",
    "decision_model",
    "created_at",
)
TABLE_COLUMNS = {
    "items": ITEM_COLUMNS,
    "clusters": CLUSTER_COLUMNS,
    "cluster_items": CLUSTER_ITEM_COLUMNS,
    "item_status": ITEM_STATUS_COLUMNS,
    "cluster_status": CLUSTER_STATUS_COLUMNS,
    "fetch_runs": FETCH_RUN_COLUMNS,
    "cluster_judge_log": JUDGE_LOG_COLUMNS,
}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def require_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:  # pragma: no cover - depends on local env
        raise SystemExit(
            "Missing psycopg. Run with:\n"
            "  uv run --with 'psycopg[binary]>=3.2' --with numpy "
            "python scripts/sync_sqlite_to_supabase_poc.py --help"
        ) from exc
    return psycopg, dict_row


def pg_connect(url: str, *, connect_timeout: int = 15):
    psycopg, dict_row = require_psycopg()
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=connect_timeout)
    conn.execute("set default_transaction_read_only=off")
    conn.commit()
    return conn


def sqlite_connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"SQLite database not found: {path}")
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_table_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["n"] if row else 0)


def sqlite_table_payload_bytes(conn: sqlite3.Connection, table: str) -> int:
    try:
        cursor = conn.execute(f"SELECT * FROM {table}")
    except sqlite3.OperationalError:
        return 0
    total = 0
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        total += sum(row_size(row) for row in rows)
    return total


def local_sync_plan(conn: sqlite3.Connection, db_path: Path, *, full: bool) -> dict[str, Any]:
    """Return a credential-free local sync plan for dry runs and preflight checks."""
    counts = {table: sqlite_table_count(conn, table) for table in SYNC_TABLES}
    item_vectors = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE embedding IS NOT NULL"
    ).fetchone()["n"]
    cluster_vectors = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters WHERE representative_vector IS NOT NULL"
    ).fetchone()["n"]
    bad_item_vectors = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE embedding IS NOT NULL AND length(embedding) != ?",
        (VECTOR_DIM * 4,),
    ).fetchone()["n"]
    bad_cluster_vectors = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM clusters
         WHERE representative_vector IS NOT NULL
           AND length(representative_vector) != ?
        """,
        (VECTOR_DIM * 4,),
    ).fetchone()["n"]
    missing_cluster_refs = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM cluster_items ci
          LEFT JOIN clusters c ON c.id = ci.cluster_id
         WHERE c.id IS NULL
        """
    ).fetchone()["n"]
    missing_item_refs = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM cluster_items ci
          LEFT JOIN items i ON i.id = ci.item_id
         WHERE i.id IS NULL
        """
    ).fetchone()["n"]
    plan = {
        "mode": "all" if full else "sample",
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "tables": counts,
        "vectors": {
            "items_with_embedding": int(item_vectors or 0),
            "clusters_with_representative_vector": int(cluster_vectors or 0),
            "bad_item_embedding_dimensions": int(bad_item_vectors or 0),
            "bad_cluster_vector_dimensions": int(bad_cluster_vectors or 0),
        },
        "referential_checks": {
            "cluster_items_missing_cluster": int(missing_cluster_refs or 0),
            "cluster_items_missing_item": int(missing_item_refs or 0),
        },
    }
    if full:
        estimated_payload_bytes = sum(
            sqlite_table_payload_bytes(conn, table)
            for table in SYNC_TABLES
            if counts.get(table, 0) > 0
        )
        plan["estimated_payload_bytes"] = estimated_payload_bytes
        plan["estimated_payload_mib"] = round(estimated_payload_bytes / 1024 / 1024, 2)
    return plan


def value_size(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    return len(str(value).encode("utf-8"))


def row_size(row: sqlite3.Row, *, skip_fields: set[str] | None = None) -> int:
    skip_fields = skip_fields or set()
    return sum(value_size(row[key]) for key in row.keys() if key not in skip_fields)


def selected_sync_plan(
    conn: sqlite3.Connection,
    db_path: Path,
    data: dict[str, list[sqlite3.Row]],
    *,
    mode: str,
    slim_days: int | None = None,
    slim_cluster_days: int | None = None,
    strip_heavy_fields: bool = False,
) -> dict[str, Any]:
    item_rows = data.get("items", [])
    cluster_rows = data.get("clusters", [])
    heavy_bytes = {
        field: sum(len(r[field] or "") for r in item_rows if field in r.keys())
        for field in HEAVY_ITEM_FIELDS
    }
    skipped = set(HEAVY_ITEM_FIELDS) if strip_heavy_fields else set()
    estimated_payload_bytes = 0
    for table, rows in data.items():
        for row in rows:
            estimated_payload_bytes += row_size(row, skip_fields=skipped if table == "items" else set())
    return {
        "mode": mode,
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "tables": {table: len(data.get(table, [])) for table in SYNC_TABLES},
        "estimated_payload_bytes": estimated_payload_bytes,
        "estimated_payload_mib": round(estimated_payload_bytes / 1024 / 1024, 2),
        "vectors": {
            "items_with_embedding": sum(1 for r in item_rows if r["embedding"] is not None),
            "clusters_with_representative_vector": sum(
                1 for r in cluster_rows if r["representative_vector"] is not None
            ),
            "bad_item_embedding_dimensions": sum(
                1
                for r in item_rows
                if r["embedding"] is not None and len(r["embedding"]) != VECTOR_DIM * 4
            ),
            "bad_cluster_vector_dimensions": sum(
                1
                for r in cluster_rows
                if r["representative_vector"] is not None
                and len(r["representative_vector"]) != VECTOR_DIM * 4
            ),
        },
        "referential_checks": {
            "cluster_items_missing_cluster": 0,
            "cluster_items_missing_item": 0,
        },
        "slim": {
            "days": slim_days,
            "cluster_days": slim_cluster_days,
            "strip_heavy_fields": strip_heavy_fields,
            "heavy_field_bytes_removed_if_stripped": heavy_bytes if strip_heavy_fields else {},
        },
    }


def checked_schema(schema: str) -> str:
    if not SCHEMA_RE.match(schema):
        raise ValueError(f"Unsafe schema name: {schema!r}")
    return schema


def checked_ident(name: str) -> str:
    if not SCHEMA_RE.match(name):
        raise ValueError(f"Unsafe identifier: {name!r}")
    return name


def quoted_columns(columns: Iterable[str]) -> str:
    return ", ".join(f'"{checked_ident(column)}"' for column in columns)


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\\u0000", "", value.replace("\x00", ""), flags=re.IGNORECASE)


def clean_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_json_value(v) for k, v in value.items()}
    return value


def bytes_to_mib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1024 / 1024, 2)


def pg_database_size_bytes(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        row = cur.execute("SELECT pg_database_size(current_database()) AS n").fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def sync_capacity_budget(
    local_plan: dict[str, Any],
    *,
    remote_db_size_bytes: int,
    max_db_mib: float | None,
    headroom_mib: float,
) -> dict[str, Any]:
    local_bytes = (
        local_plan.get("estimated_payload_bytes")
        if local_plan.get("estimated_payload_bytes") is not None
        else local_plan.get("db_size_bytes")
    ) or 0
    headroom_bytes = max(0, int((headroom_mib or 0) * 1024 * 1024))
    rough_after = int(remote_db_size_bytes or 0) + int(local_bytes)
    budget: dict[str, Any] = {
        "local_bytes_basis": (
            "estimated_payload_bytes"
            if local_plan.get("estimated_payload_bytes") is not None
            else "sqlite_file_size"
        ),
        "local_payload_bytes": int(local_bytes),
        "remote_db_size_bytes": int(remote_db_size_bytes or 0),
        "rough_after_sync_bytes": rough_after,
        "rough_after_sync_mib": bytes_to_mib(rough_after),
        "headroom_mib": headroom_mib,
    }
    if max_db_mib and max_db_mib > 0:
        max_bytes = int(max_db_mib * 1024 * 1024)
        budget["max_db_mib"] = max_db_mib
        budget["would_exceed_max"] = rough_after + headroom_bytes > max_bytes
    else:
        budget["max_db_mib"] = None
        budget["would_exceed_max"] = None
    return budget


def assert_capacity_budget(
    local_plan: dict[str, Any],
    *,
    remote_db_size_bytes: int,
    max_db_mib: float | None,
    headroom_mib: float,
) -> dict[str, Any]:
    budget = sync_capacity_budget(
        local_plan,
        remote_db_size_bytes=remote_db_size_bytes,
        max_db_mib=max_db_mib,
        headroom_mib=headroom_mib,
    )
    if budget.get("would_exceed_max"):
        raise SystemExit(
            "capacity check failed before remote writes: "
            f"rough_after_sync_mib={budget['rough_after_sync_mib']}, "
            f"headroom_mib={budget['headroom_mib']}, "
            f"max_db_mib={budget['max_db_mib']}. "
            "Increase --max-db-mib after upgrading capacity, or sync a smaller profile."
        )
    return budget


def jsonb(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(clean_json_value(raw), ensure_ascii=False)
    if not isinstance(raw, str):
        return json.dumps(clean_json_value(raw), ensure_ascii=False)
    try:
        parsed = json.loads(clean_text(raw) or "")
    except Exception:
        return None
    return json.dumps(clean_json_value(parsed), ensure_ascii=False)


def boolish(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def vector_literal(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.shape[0] != VECTOR_DIM:
        return None
    return "[" + ",".join(f"{float(x):.8g}" for x in arr) + "]"


def pg_timestamp(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"\d{10}(\.\d+)?", stripped):
            return datetime.fromtimestamp(float(stripped), tz=timezone.utc).isoformat()
        if re.fullmatch(r"\d{13}", stripped):
            return datetime.fromtimestamp(float(stripped) / 1000, tz=timezone.utc).isoformat()
        return stripped
    return value


def chunks(values: list[Any], size: int = 500):
    for idx in range(0, len(values), size):
        yield values[idx:idx + size]


def rows_by_ids(conn: sqlite3.Connection, table: str, ids: list[Any], id_col: str = "id") -> list[sqlite3.Row]:
    if not ids:
        return []
    rows: list[sqlite3.Row] = []
    for batch in chunks(ids):
        rows.extend(conn.execute(
            f"SELECT * FROM {table} WHERE {id_col} IN ({placeholders(batch)})",
            tuple(batch),
        ).fetchall())
    return rows


def build_by_ids(
    conn: sqlite3.Connection,
    *,
    item_ids: list[str],
    cluster_ids: list[int],
    judge_log_limit: int | None = 1000,
) -> dict[str, list[sqlite3.Row]]:
    items = rows_by_ids(conn, "items", item_ids)
    clusters = rows_by_ids(conn, "clusters", cluster_ids)
    item_id_set = set(item_ids)
    cluster_items = []
    if cluster_ids and item_ids:
        for cluster_batch in chunks(cluster_ids):
            rows = conn.execute(
                f"""
                SELECT *
                  FROM cluster_items
                 WHERE cluster_id IN ({placeholders(cluster_batch)})
                 ORDER BY cluster_id, COALESCE(rank_in_cluster, 9999), item_id
                """,
                tuple(cluster_batch),
            ).fetchall()
            cluster_items.extend([r for r in rows if r["item_id"] in item_id_set])
    fetch_run_ids = [
        int(r["fetch_run_id"])
        for r in items
        if "fetch_run_id" in r.keys() and r["fetch_run_id"] is not None
    ]
    fetch_runs = rows_by_ids(conn, "fetch_runs", sorted(set(fetch_run_ids)))
    if cluster_ids:
        cluster_status = []
        for cluster_batch in chunks(cluster_ids):
            cluster_status.extend(conn.execute(
                f"""
                SELECT *
                  FROM cluster_status
                 WHERE cluster_id IN ({placeholders(cluster_batch)})
                 ORDER BY user_id, cluster_id
                """,
                tuple(cluster_batch),
            ).fetchall())
    else:
        cluster_status = []
    if item_ids:
        item_status = []
        for item_batch in chunks(item_ids):
            item_status.extend(conn.execute(
                f"""
                SELECT *
                  FROM item_status
                 WHERE item_id IN ({placeholders(item_batch)})
                 ORDER BY user_id, item_id
                """,
                tuple(item_batch),
            ).fetchall())
        limit_sql = "" if judge_log_limit is None else f"LIMIT {int(judge_log_limit)}"
        judge_logs = []
        for item_batch in chunks(item_ids):
            judge_logs.extend(conn.execute(
                f"""
                SELECT *
                  FROM cluster_judge_log
                 WHERE item_id IN ({placeholders(item_batch)})
                 ORDER BY id
                 {limit_sql}
                """,
                tuple(item_batch),
            ).fetchall())
            if judge_log_limit is not None and len(judge_logs) >= judge_log_limit:
                judge_logs = judge_logs[:judge_log_limit]
                break
    else:
        item_status = []
        judge_logs = []
    return {
        "items": items,
        "clusters": clusters,
        "cluster_items": cluster_items,
        "item_status": item_status,
        "cluster_status": cluster_status,
        "fetch_runs": fetch_runs,
        "cluster_judge_log": judge_logs,
    }


def build_sample(conn: sqlite3.Connection, *, items_limit: int, clusters_limit: int) -> dict[str, list[sqlite3.Row]]:
    item_ids, cluster_ids = sample_ids(conn, items_limit=items_limit, clusters_limit=clusters_limit)
    return build_by_ids(conn, item_ids=item_ids, cluster_ids=cluster_ids, judge_log_limit=1000)


def build_slim(
    conn: sqlite3.Connection,
    *,
    days: int,
    cluster_days: int,
    recent_items_limit: int,
    judge_log_limit: int | None = None,
) -> dict[str, list[sqlite3.Row]]:
    cluster_rows = conn.execute(
        """
        SELECT id
          FROM clusters
         WHERE is_visible_in_feed = 1
           AND published_at IS NOT NULL
           AND COALESCE(last_updated_at, published_at, first_doc_at, created_at) >= datetime('now', ?)
         ORDER BY COALESCE(published_at, last_updated_at, first_doc_at, created_at) DESC, id DESC
        """,
        (f"-{int(cluster_days)} days",),
    ).fetchall()
    cluster_ids = [int(r["id"]) for r in cluster_rows]
    member_item_ids: list[str] = []
    if cluster_ids:
        for cluster_batch in chunks(cluster_ids):
            member_item_ids.extend([
                str(r["item_id"])
                for r in conn.execute(
                f"""
                SELECT item_id
                  FROM cluster_items
                 WHERE cluster_id IN ({placeholders(cluster_batch)})
                 ORDER BY cluster_id, COALESCE(rank_in_cluster, 9999), item_id
                """,
                    tuple(cluster_batch),
                ).fetchall()
            ])
    recent_limit_sql = "" if recent_items_limit <= 0 else "LIMIT ?"
    params: tuple[Any, ...] = (f"-{int(days)} days",)
    if recent_items_limit > 0:
        params = (*params, int(recent_items_limit))
    recent_item_ids = [
        str(r["id"])
        for r in conn.execute(
            f"""
            SELECT id
              FROM items
             WHERE fetched_at >= datetime('now', ?)
             ORDER BY fetched_at DESC, id DESC
             {recent_limit_sql}
            """,
            params,
        ).fetchall()
    ]
    item_ids = ordered_unique([*member_item_ids, *recent_item_ids])
    return build_by_ids(
        conn,
        item_ids=item_ids,
        cluster_ids=cluster_ids,
        judge_log_limit=judge_log_limit,
    )


def build_incremental(
    conn: sqlite3.Connection,
    *,
    hours: int,
    recent_items_limit: int,
    judge_log_limit: int | None = 5000,
) -> dict[str, list[sqlite3.Row]]:
    lookback = f"-{int(hours)} hours"
    cluster_rows = conn.execute(
        """
        SELECT id
          FROM clusters
         WHERE COALESCE(last_updated_at, published_at, first_doc_at, created_at) >= datetime('now', ?)
         ORDER BY COALESCE(last_updated_at, published_at, first_doc_at, created_at) DESC, id DESC
        """,
        (lookback,),
    ).fetchall()
    cluster_ids = [int(r["id"]) for r in cluster_rows]
    member_item_ids: list[str] = []
    if cluster_ids:
        for cluster_batch in chunks(cluster_ids):
            member_item_ids.extend([
                str(r["item_id"])
                for r in conn.execute(
                    f"""
                    SELECT item_id
                      FROM cluster_items
                     WHERE cluster_id IN ({placeholders(cluster_batch)})
                     ORDER BY cluster_id, COALESCE(rank_in_cluster, 9999), item_id
                    """,
                    tuple(cluster_batch),
                ).fetchall()
            ])
    recent_limit_sql = "" if recent_items_limit <= 0 else "LIMIT ?"
    params: tuple[Any, ...] = (lookback,)
    if recent_items_limit > 0:
        params = (*params, int(recent_items_limit))
    recent_item_ids = [
        str(r["id"])
        for r in conn.execute(
            f"""
            SELECT id
              FROM items
             WHERE fetched_at >= datetime('now', ?)
             ORDER BY fetched_at DESC, id DESC
             {recent_limit_sql}
            """,
            params,
        ).fetchall()
    ]
    return build_by_ids(
        conn,
        item_ids=ordered_unique([*member_item_ids, *recent_item_ids]),
        cluster_ids=cluster_ids,
        judge_log_limit=judge_log_limit,
    )


def execute_schema(pg_conn, migration_path: Path) -> None:
    sql = migration_path.read_text(encoding="utf-8")
    with pg_conn.cursor() as cur:
        cur.execute(sql)
    pg_conn.commit()


def iter_batches(cursor: sqlite3.Cursor, batch_size: int):
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        yield rows


def upsert_in_batches(
    sqlite_conn: sqlite3.Connection,
    sql: str,
    upsert_fn,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
) -> int:
    total = 0
    cursor = sqlite_conn.execute(sql)
    for rows in iter_batches(cursor, batch_size):
        total += upsert_fn(pg_conn, rows, schema)
        pg_conn.commit()
    return total


def upsert_rows_in_batches(
    pg_conn,
    rows: list[sqlite3.Row],
    upsert_fn,
    schema: str,
    *,
    batch_size: int,
    **kwargs,
) -> int:
    total = 0
    for idx in range(0, len(rows), batch_size):
        total += upsert_fn(pg_conn, rows[idx:idx + batch_size], schema, **kwargs)
        pg_conn.commit()
    return total


def all_items_select_sql(items_offset: int = 0) -> str:
    sql = "SELECT * FROM items ORDER BY fetched_at DESC, id DESC"
    if items_offset > 0:
        sql += f" LIMIT -1 OFFSET {int(items_offset)}"
    return sql


def all_item_ids(
    conn: sqlite3.Connection,
    *,
    items_offset: int = 0,
    items_limit: int | None = None,
) -> list[str]:
    sql = "SELECT id FROM items ORDER BY fetched_at DESC, id DESC"
    if items_limit is not None and items_limit > 0:
        sql += f" LIMIT {int(items_limit)}"
        if items_offset > 0:
            sql += f" OFFSET {int(items_offset)}"
    elif items_offset > 0:
        sql += f" LIMIT -1 OFFSET {int(items_offset)}"
    return [str(r["id"] if isinstance(r, sqlite3.Row) else r[0]) for r in conn.execute(sql).fetchall()]


def all_table_select_sql(table: str) -> str:
    if table == "clusters":
        return "SELECT * FROM clusters ORDER BY id"
    if table == "cluster_items":
        return "SELECT * FROM cluster_items ORDER BY cluster_id, item_id"
    if table == "item_status":
        return (
            "select s.* from item_status s "
            "join items i on i.id = s.item_id "
            "order by s.user_id, s.item_id"
        )
    if table == "cluster_status":
        return (
            "select s.* from cluster_status s "
            "join clusters c on c.id = s.cluster_id "
            "order by s.user_id, s.cluster_id"
        )
    if table == "fetch_runs":
        return "SELECT * FROM fetch_runs ORDER BY id"
    if table == "cluster_judge_log":
        return "SELECT * FROM cluster_judge_log ORDER BY id"
    raise ValueError(f"Unsupported full sync table: {table}")


def upsert_all_items_by_id_batches(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
    items_offset: int = 0,
    items_limit: int | None = None,
) -> int:
    item_ids = all_item_ids(sqlite_conn, items_offset=items_offset, items_limit=items_limit)
    total = 0
    for id_batch in chunks(item_ids, batch_size):
        rows = rows_by_ids(sqlite_conn, "items", id_batch)
        total += upsert_items(pg_conn, rows, schema)
        pg_conn.commit()
    return total


def row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        value = row.get(key, default)
        return clean_text(value) if isinstance(value, str) else value
    try:
        if key in row.keys():
            value = row[key]
            return clean_text(value) if isinstance(value, str) else value
    except Exception:
        pass
    return default


def item_payload(row: Any, *, slim: bool = False, skip_embedding: bool = False) -> dict[str, Any]:
    content = None if slim else row_value(row, "content")
    detail_json = None if slim else jsonb(row_value(row, "detail_json"))
    comments_json = None if slim else jsonb(row_value(row, "comments_json"))
    return {
        "id": row_value(row, "id"),
        "user_id": row_value(row, "user_id"),
        "platform": row_value(row, "platform"),
        "source": row_value(row, "source"),
        "fetch_run_id": row_value(row, "fetch_run_id"),
        "title": row_value(row, "title"),
        "content": content,
        "author_name": row_value(row, "author_name"),
        "author_id": row_value(row, "author_id"),
        "author_avatar": row_value(row, "author_avatar"),
        "url": row_value(row, "url"),
        "cover_url": row_value(row, "cover_url"),
        "description": row_value(row, "description"),
        "media_json": jsonb(row_value(row, "media_json")),
        "metrics_json": jsonb(row_value(row, "metrics_json")),
        "tags_json": jsonb(row_value(row, "tags_json")),
        "lang": row_value(row, "lang"),
        "detail_json": detail_json,
        "comments_json": comments_json,
        "ai_summary": row_value(row, "ai_summary"),
        "ai_key_points": row_value(row, "ai_key_points"),
        "ai_category": row_value(row, "ai_category"),
        "ai_keywords": row_value(row, "ai_keywords"),
        "ai_categories": jsonb(row_value(row, "ai_categories")),
        "ai_subcategories": jsonb(row_value(row, "ai_subcategories")),
        "multi_l1_reason": row_value(row, "multi_l1_reason"),
        "ai_extracted": jsonb(row_value(row, "ai_extracted")),
        "content_type": row_value(row, "content_type"),
        "ai_quality_score": row_value(row, "ai_quality_score"),
        "visible": row_value(row, "visible"),
        "relevance_score": row_value(row, "relevance_score"),
        "embedding": None if skip_embedding else vector_literal(row_value(row, "embedding")),
        "embedding_provider": row_value(row, "embedding_provider"),
        "embedding_model": row_value(row, "embedding_model"),
        "embedding_input_variant": row_value(row, "embedding_input_variant"),
        "embedding_generated_at": pg_timestamp(row_value(row, "embedding_generated_at")),
        "canonical_url": row_value(row, "canonical_url"),
        "cluster_id": row_value(row, "cluster_id"),
        "fetched_at": pg_timestamp(row_value(row, "fetched_at")),
        "published_at": pg_timestamp(row_value(row, "published_at")),
        "created_at": pg_timestamp(row_value(row, "created_at")),
    }


def cluster_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row_value(row, "id"),
        "ai_title": row_value(row, "ai_title"),
        "ai_summary": row_value(row, "ai_summary"),
        "ai_key_points": row_value(row, "ai_key_points"),
        "live_version": row_value(row, "live_version"),
        "doc_count": row_value(row, "doc_count"),
        "unique_source_count": row_value(row, "unique_source_count"),
        "platforms_json": jsonb(row_value(row, "platforms_json")),
        "cover_url": row_value(row, "cover_url"),
        "first_doc_at": pg_timestamp(row_value(row, "first_doc_at")),
        "last_doc_at": pg_timestamp(row_value(row, "last_doc_at")),
        "last_updated_at": pg_timestamp(row_value(row, "last_updated_at")),
        "is_visible_in_feed": boolish(row_value(row, "is_visible_in_feed")),
        "merged_into": row_value(row, "merged_into"),
        "archived": boolish(row_value(row, "archived")),
        "prompt_version": row_value(row, "prompt_version"),
        "representative_vector": vector_literal(row_value(row, "representative_vector")),
        "event_embedding": vector_literal(row_value(row, "event_embedding")),
        "created_run_id": row_value(row, "created_run_id"),
        "last_touched_run_id": row_value(row, "last_touched_run_id"),
        "published_run_id": row_value(row, "published_run_id"),
        "published_at": pg_timestamp(row_value(row, "published_at")),
        "created_at": pg_timestamp(row_value(row, "created_at")),
    }


def cluster_item_payload(row: Any) -> dict[str, Any]:
    return {
        "cluster_id": row_value(row, "cluster_id"),
        "item_id": row_value(row, "item_id"),
        "rank_in_cluster": row_value(row, "rank_in_cluster"),
        "added_at": pg_timestamp(row_value(row, "added_at")),
        "is_primary_source": boolish(row_value(row, "is_primary_source")),
        "source_identity": row_value(row, "source_identity"),
        "join_decision_id": row_value(row, "join_decision_id"),
    }


def item_status_payload(row: Any) -> dict[str, Any]:
    return {
        "user_id": row_value(row, "user_id"),
        "item_id": row_value(row, "item_id"),
        "read_at": pg_timestamp(row_value(row, "read_at")),
        "clicked_at": pg_timestamp(row_value(row, "clicked_at")),
        "starred_at": pg_timestamp(row_value(row, "starred_at")),
        "hidden_at": pg_timestamp(row_value(row, "hidden_at")),
    }


def cluster_status_payload(row: Any) -> dict[str, Any]:
    return {
        "user_id": row_value(row, "user_id"),
        "cluster_id": row_value(row, "cluster_id"),
        "clicked_at": pg_timestamp(row_value(row, "clicked_at")),
        "starred_at": pg_timestamp(row_value(row, "starred_at")),
        "last_seen_version": row_value(row, "last_seen_version"),
    }


def fetch_run_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row_value(row, "id"),
        "started_at": pg_timestamp(row_value(row, "started_at")),
        "finished_at": pg_timestamp(row_value(row, "finished_at")),
        "status": row_value(row, "status"),
        "stats_json": jsonb(row_value(row, "stats_json")),
        "error_msg": row_value(row, "error_msg"),
    }


def judge_log_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row_value(row, "id"),
        "item_id": row_value(row, "item_id"),
        "candidate_cluster_ids": jsonb(row_value(row, "candidate_cluster_ids")),
        "llm_input_tokens": row_value(row, "llm_input_tokens"),
        "llm_output_tokens": row_value(row, "llm_output_tokens"),
        "matches_json": jsonb(row_value(row, "matches_json")),
        "selected_cluster_id": row_value(row, "selected_cluster_id"),
        "selection_reason": row_value(row, "selection_reason"),
        "possible_merge_candidates": jsonb(row_value(row, "possible_merge_candidates")),
        "decision_model": row_value(row, "decision_model"),
        "created_at": pg_timestamp(row_value(row, "created_at")),
    }


def table_payload(table: str, row: Any, *, slim: bool = False) -> dict[str, Any]:
    if table == "items":
        return item_payload(row, slim=slim)
    if table == "clusters":
        return cluster_payload(row)
    if table == "cluster_items":
        return cluster_item_payload(row)
    if table == "item_status":
        return item_status_payload(row)
    if table == "cluster_status":
        return cluster_status_payload(row)
    if table == "fetch_runs":
        return fetch_run_payload(row)
    if table == "cluster_judge_log":
        return judge_log_payload(row)
    raise ValueError(f"Unsupported sync table: {table}")


def copy_row_values(row: Any, *, columns: Iterable[str], payload_fn, **kwargs) -> tuple[Any, ...]:
    payload = payload_fn(row, **kwargs)
    return tuple(payload.get(column) for column in columns)


def copy_command_sql(stage_table: str, columns: Iterable[str]) -> str:
    return f"COPY {checked_ident(stage_table)} ({quoted_columns(columns)}) FROM STDIN"


def create_stage_table_sql(schema: str, table: str, stage_table: str) -> str:
    checked_schema(schema)
    if table not in TABLE_COLUMNS:
        raise ValueError(f"Unsupported sync table: {table}")
    return (
        f"CREATE TEMP TABLE {checked_ident(stage_table)} "
        f"(LIKE {schema}.{checked_ident(table)} INCLUDING DEFAULTS) ON COMMIT DROP"
    )


def bulk_update_assignments(table: str) -> str:
    if table == "items":
        return """
          user_id = excluded.user_id,
          platform = excluded.platform,
          source = excluded.source,
          fetch_run_id = excluded.fetch_run_id,
          title = excluded.title,
          content = COALESCE(excluded.content, target.content),
          author_name = excluded.author_name,
          author_id = excluded.author_id,
          author_avatar = excluded.author_avatar,
          url = excluded.url,
          cover_url = excluded.cover_url,
          description = excluded.description,
          media_json = excluded.media_json,
          metrics_json = excluded.metrics_json,
          tags_json = excluded.tags_json,
          lang = excluded.lang,
          detail_json = COALESCE(excluded.detail_json, target.detail_json),
          comments_json = COALESCE(excluded.comments_json, target.comments_json),
          ai_summary = excluded.ai_summary,
          ai_key_points = excluded.ai_key_points,
          ai_category = excluded.ai_category,
          ai_keywords = excluded.ai_keywords,
          ai_categories = excluded.ai_categories,
          ai_subcategories = excluded.ai_subcategories,
          multi_l1_reason = excluded.multi_l1_reason,
          ai_extracted = excluded.ai_extracted,
          content_type = excluded.content_type,
          ai_quality_score = excluded.ai_quality_score,
          visible = excluded.visible,
          relevance_score = excluded.relevance_score,
          embedding = COALESCE(target.embedding, excluded.embedding),
          embedding_provider = COALESCE(target.embedding_provider, excluded.embedding_provider),
          embedding_model = COALESCE(target.embedding_model, excluded.embedding_model),
          embedding_input_variant = COALESCE(target.embedding_input_variant, excluded.embedding_input_variant),
          embedding_generated_at = COALESCE(target.embedding_generated_at, excluded.embedding_generated_at),
          canonical_url = excluded.canonical_url,
          cluster_id = excluded.cluster_id,
          fetched_at = excluded.fetched_at,
          published_at = excluded.published_at,
          created_at = excluded.created_at
        """
    if table == "clusters":
        return """
          ai_title = excluded.ai_title,
          ai_summary = excluded.ai_summary,
          ai_key_points = excluded.ai_key_points,
          live_version = excluded.live_version,
          doc_count = excluded.doc_count,
          unique_source_count = excluded.unique_source_count,
          platforms_json = excluded.platforms_json,
          cover_url = excluded.cover_url,
          first_doc_at = excluded.first_doc_at,
          last_doc_at = excluded.last_doc_at,
          last_updated_at = excluded.last_updated_at,
          is_visible_in_feed = excluded.is_visible_in_feed,
          merged_into = excluded.merged_into,
          archived = excluded.archived,
          prompt_version = excluded.prompt_version,
          representative_vector = COALESCE(excluded.representative_vector, target.representative_vector),
          event_embedding = COALESCE(excluded.event_embedding, target.event_embedding),
          created_run_id = excluded.created_run_id,
          last_touched_run_id = excluded.last_touched_run_id,
          published_run_id = excluded.published_run_id,
          published_at = excluded.published_at,
          created_at = excluded.created_at
        """
    if table == "cluster_items":
        return """
          rank_in_cluster = excluded.rank_in_cluster,
          added_at = excluded.added_at,
          is_primary_source = excluded.is_primary_source,
          source_identity = excluded.source_identity,
          join_decision_id = excluded.join_decision_id
        """
    if table == "item_status":
        return """
          read_at = excluded.read_at,
          clicked_at = excluded.clicked_at,
          starred_at = excluded.starred_at,
          hidden_at = excluded.hidden_at
        """
    if table == "cluster_status":
        return """
          clicked_at = excluded.clicked_at,
          last_seen_version = excluded.last_seen_version
        """
    if table == "fetch_runs":
        return """
          started_at = excluded.started_at,
          finished_at = excluded.finished_at,
          status = excluded.status,
          stats_json = excluded.stats_json,
          error_msg = excluded.error_msg
        """
    if table == "cluster_judge_log":
        return """
          item_id = excluded.item_id,
          candidate_cluster_ids = excluded.candidate_cluster_ids,
          llm_input_tokens = excluded.llm_input_tokens,
          llm_output_tokens = excluded.llm_output_tokens,
          matches_json = excluded.matches_json,
          selected_cluster_id = excluded.selected_cluster_id,
          selection_reason = excluded.selection_reason,
          possible_merge_candidates = excluded.possible_merge_candidates,
          decision_model = excluded.decision_model,
          created_at = excluded.created_at
        """
    raise ValueError(f"Unsupported sync table: {table}")


def conflict_target(table: str) -> str:
    if table in {"items", "clusters", "fetch_runs", "cluster_judge_log"}:
        return "id"
    if table in {"cluster_items", "cluster_status"}:
        return "cluster_id, item_id" if table == "cluster_items" else "user_id, cluster_id"
    if table == "item_status":
        return "user_id, item_id"
    raise ValueError(f"Unsupported sync table: {table}")


def bulk_merge_sql(schema: str, table: str, stage_table: str) -> str:
    checked_schema(schema)
    if table not in TABLE_COLUMNS:
        raise ValueError(f"Unsupported sync table: {table}")
    columns = TABLE_COLUMNS[table]
    col_sql = quoted_columns(columns)
    assignments = bulk_update_assignments(table)
    return f"""
        insert into {schema}.{checked_ident(table)} as target ({col_sql})
        select {col_sql}
          from {checked_ident(stage_table)}
        on conflict ({conflict_target(table)}) do update set
        {assignments}
    """


def bulk_copy_rows(
    pg_conn,
    rows: Iterable[Any],
    schema: str,
    table: str,
    *,
    slim: bool = False,
    skip_item_embedding_ids: set[str] | None = None,
) -> int:
    row_list = list(rows)
    if not row_list:
        return 0
    columns = TABLE_COLUMNS[table]
    stage_table = f"stage_{checked_ident(table)}"
    skip_item_embedding_ids = skip_item_embedding_ids or set()
    with pg_conn.cursor() as cur:
        cur.execute(create_stage_table_sql(schema, table, stage_table))
        with cur.copy(copy_command_sql(stage_table, columns)) as copy:
            for row in row_list:
                if table == "items":
                    skip_embedding = str(row_value(row, "id")) in skip_item_embedding_ids
                    values = copy_row_values(
                        row,
                        columns=columns,
                        payload_fn=item_payload,
                        slim=slim,
                        skip_embedding=skip_embedding,
                    )
                else:
                    values = copy_row_values(
                        row,
                        columns=columns,
                        payload_fn=lambda value, **_: table_payload(table, value, slim=slim),
                    )
                copy.write_row(
                    values
                )
        cur.execute(bulk_merge_sql(schema, table, stage_table))
    return len(row_list)


def bulk_copy_in_batches(
    sqlite_conn: sqlite3.Connection,
    sql: str,
    pg_conn,
    schema: str,
    table: str,
    *,
    batch_size: int,
    slim: bool = False,
) -> int:
    total = 0
    cursor = sqlite_conn.execute(sql)
    for rows in iter_batches(cursor, batch_size):
        try:
            total += bulk_copy_rows(pg_conn, rows, schema, table, slim=slim)
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise
    return total


def bulk_copy_rows_in_batches(
    pg_conn,
    rows: list[Any],
    schema: str,
    table: str,
    *,
    batch_size: int,
    slim: bool = False,
) -> int:
    total = 0
    for row_batch in chunks(rows, batch_size):
        try:
            total += bulk_copy_rows(pg_conn, row_batch, schema, table, slim=slim)
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise
    return total


def remote_item_ids_with_embedding(pg_conn, schema: str, item_ids: list[str]) -> set[str]:
    if not item_ids:
        return set()
    checked_schema(schema)
    found: set[str] = set()
    with pg_conn.cursor() as cur:
        for id_batch in chunks(item_ids, 1000):
            rows = cur.execute(
                f"""
                select id
                  from {schema}.items
                 where embedding is not null
                   and id = any(%s)
                """,
                (list(id_batch),),
            ).fetchall()
            for row in rows:
                found.add(str(row["id"] if isinstance(row, dict) else row[0]))
    return found


def bulk_copy_all_items_by_id_batches(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
    items_offset: int = 0,
    items_limit: int | None = None,
) -> int:
    item_ids = all_item_ids(sqlite_conn, items_offset=items_offset, items_limit=items_limit)
    total = 0
    for id_batch in chunks(item_ids, batch_size):
        rows = rows_by_ids(sqlite_conn, "items", id_batch)
        existing_embedding_ids = remote_item_ids_with_embedding(pg_conn, schema, id_batch)
        try:
            total += bulk_copy_rows(
                pg_conn,
                rows,
                schema,
                "items",
                skip_item_embedding_ids=existing_embedding_ids,
            )
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise
    return total


def sync_selected_rows(
    pg_conn,
    data: dict[str, list[Any]],
    schema: str,
    *,
    batch_size: int,
    bulk_copy: bool,
    slim_items: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if bulk_copy:
        counts["items"] = bulk_copy_rows_in_batches(
            pg_conn, data["items"], schema, "items", batch_size=batch_size, slim=slim_items
        )
        counts["clusters"] = bulk_copy_rows_in_batches(
            pg_conn, data["clusters"], schema, "clusters", batch_size=batch_size
        )
        counts["cluster_items"] = bulk_copy_rows_in_batches(
            pg_conn, data["cluster_items"], schema, "cluster_items", batch_size=batch_size
        )
        counts["item_status"] = bulk_copy_rows_in_batches(
            pg_conn, data["item_status"], schema, "item_status", batch_size=batch_size
        )
        counts["cluster_status"] = bulk_copy_rows_in_batches(
            pg_conn, data["cluster_status"], schema, "cluster_status", batch_size=batch_size
        )
        counts["fetch_runs"] = bulk_copy_rows_in_batches(
            pg_conn, data["fetch_runs"], schema, "fetch_runs", batch_size=batch_size
        )
        counts["cluster_judge_log"] = bulk_copy_rows_in_batches(
            pg_conn, data["cluster_judge_log"], schema, "cluster_judge_log", batch_size=batch_size
        )
        return counts

    counts["items"] = upsert_rows_in_batches(
        pg_conn, data["items"], upsert_items, schema, batch_size=batch_size, slim=slim_items
    )
    counts["clusters"] = upsert_rows_in_batches(
        pg_conn, data["clusters"], upsert_clusters, schema, batch_size=batch_size
    )
    counts["cluster_items"] = upsert_rows_in_batches(
        pg_conn, data["cluster_items"], upsert_cluster_items, schema, batch_size=batch_size
    )
    counts["item_status"] = upsert_rows_in_batches(
        pg_conn, data["item_status"], upsert_item_status, schema, batch_size=batch_size
    )
    counts["cluster_status"] = upsert_rows_in_batches(
        pg_conn, data["cluster_status"], upsert_cluster_status, schema, batch_size=batch_size
    )
    counts["fetch_runs"] = upsert_rows_in_batches(
        pg_conn, data["fetch_runs"], upsert_fetch_runs, schema, batch_size=batch_size
    )
    counts["cluster_judge_log"] = upsert_rows_in_batches(
        pg_conn, data["cluster_judge_log"], upsert_judge_logs, schema, batch_size=batch_size
    )
    return counts


def upsert_items(pg_conn, rows: Iterable[sqlite3.Row], schema: str, *, slim: bool = False) -> int:
    sql = f"""
        insert into {schema}.items (
          id, user_id, platform, source, fetch_run_id, title, content,
          author_name, author_id, author_avatar, url, cover_url,
          description, media_json, metrics_json, tags_json, lang, detail_json,
          comments_json, ai_summary, ai_key_points, ai_category, ai_keywords,
          ai_categories, ai_subcategories, multi_l1_reason, ai_extracted,
          content_type, ai_quality_score, visible, relevance_score, embedding,
          embedding_provider, embedding_model, embedding_input_variant, embedding_generated_at,
          canonical_url, cluster_id, fetched_at, published_at, created_at
        )
        values (
          %(id)s, %(user_id)s, %(platform)s, %(source)s, %(fetch_run_id)s,
          %(title)s, %(content)s, %(author_name)s, %(author_id)s,
          %(author_avatar)s, %(url)s, %(cover_url)s, %(description)s,
          %(media_json)s::jsonb, %(metrics_json)s::jsonb, %(tags_json)s::jsonb, %(lang)s,
          %(detail_json)s::jsonb, %(comments_json)s::jsonb, %(ai_summary)s,
          %(ai_key_points)s, %(ai_category)s, %(ai_keywords)s,
          %(ai_categories)s::jsonb, %(ai_subcategories)s::jsonb,
          %(multi_l1_reason)s, %(ai_extracted)s::jsonb, %(content_type)s,
          %(ai_quality_score)s, %(visible)s, %(relevance_score)s,
          %(embedding)s::{VECTOR_SQL_TYPE}, %(embedding_provider)s, %(embedding_model)s,
          %(embedding_input_variant)s, %(embedding_generated_at)s,
          %(canonical_url)s, %(cluster_id)s, %(fetched_at)s,
          %(published_at)s, %(created_at)s
        )
        on conflict (id) do update set
          user_id = excluded.user_id,
          platform = excluded.platform,
          source = excluded.source,
          fetch_run_id = excluded.fetch_run_id,
          title = excluded.title,
          content = COALESCE(excluded.content, items.content),
          author_name = excluded.author_name,
          author_id = excluded.author_id,
          author_avatar = excluded.author_avatar,
          url = excluded.url,
          cover_url = excluded.cover_url,
          description = excluded.description,
          media_json = excluded.media_json,
          metrics_json = excluded.metrics_json,
          tags_json = excluded.tags_json,
          lang = excluded.lang,
          detail_json = COALESCE(excluded.detail_json, items.detail_json),
          comments_json = COALESCE(excluded.comments_json, items.comments_json),
          ai_summary = excluded.ai_summary,
          ai_key_points = excluded.ai_key_points,
          ai_category = excluded.ai_category,
          ai_keywords = excluded.ai_keywords,
          ai_categories = excluded.ai_categories,
          ai_subcategories = excluded.ai_subcategories,
          multi_l1_reason = excluded.multi_l1_reason,
          ai_extracted = excluded.ai_extracted,
          content_type = excluded.content_type,
          ai_quality_score = excluded.ai_quality_score,
          visible = excluded.visible,
          relevance_score = excluded.relevance_score,
          embedding = COALESCE(items.embedding, excluded.embedding),
          embedding_provider = COALESCE(items.embedding_provider, excluded.embedding_provider),
          embedding_model = COALESCE(items.embedding_model, excluded.embedding_model),
          embedding_input_variant = COALESCE(items.embedding_input_variant, excluded.embedding_input_variant),
          embedding_generated_at = COALESCE(items.embedding_generated_at, excluded.embedding_generated_at),
          canonical_url = excluded.canonical_url,
          cluster_id = excluded.cluster_id,
          fetched_at = excluded.fetched_at,
          published_at = excluded.published_at,
          created_at = excluded.created_at
    """
    payload = [item_payload(r, slim=slim) for r in rows]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_clusters(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.clusters (
          id, ai_title, ai_summary, ai_key_points, live_version, doc_count,
          unique_source_count, platforms_json, cover_url, first_doc_at,
          last_doc_at, last_updated_at, is_visible_in_feed, merged_into,
          archived, prompt_version, representative_vector, event_embedding,
          created_run_id, last_touched_run_id, published_run_id,
          published_at, created_at
        )
        values (
          %(id)s, %(ai_title)s, %(ai_summary)s, %(ai_key_points)s,
          %(live_version)s, %(doc_count)s, %(unique_source_count)s,
          %(platforms_json)s::jsonb, %(cover_url)s, %(first_doc_at)s,
          %(last_doc_at)s, %(last_updated_at)s, %(is_visible_in_feed)s,
          %(merged_into)s, %(archived)s, %(prompt_version)s,
          %(representative_vector)s::{VECTOR_SQL_TYPE},
          %(event_embedding)s::{VECTOR_SQL_TYPE}, %(created_run_id)s,
          %(last_touched_run_id)s, %(published_run_id)s, %(published_at)s,
          %(created_at)s
        )
        on conflict (id) do update set
          ai_title = excluded.ai_title,
          ai_summary = excluded.ai_summary,
          ai_key_points = excluded.ai_key_points,
          live_version = excluded.live_version,
          doc_count = excluded.doc_count,
          unique_source_count = excluded.unique_source_count,
          platforms_json = excluded.platforms_json,
          representative_vector = COALESCE(excluded.representative_vector, clusters.representative_vector),
          event_embedding = COALESCE(excluded.event_embedding, clusters.event_embedding),
          last_updated_at = excluded.last_updated_at,
          is_visible_in_feed = excluded.is_visible_in_feed,
          published_run_id = excluded.published_run_id,
          published_at = excluded.published_at
    """
    payload = []
    for r in rows:
        payload.append({
            "id": r["id"],
            "ai_title": r["ai_title"],
            "ai_summary": r["ai_summary"],
            "ai_key_points": r["ai_key_points"],
            "live_version": r["live_version"],
            "doc_count": r["doc_count"],
            "unique_source_count": r["unique_source_count"],
            "platforms_json": jsonb(r["platforms_json"]),
            "cover_url": r["cover_url"],
            "first_doc_at": pg_timestamp(r["first_doc_at"]),
            "last_doc_at": pg_timestamp(r["last_doc_at"]),
            "last_updated_at": pg_timestamp(r["last_updated_at"]),
            "is_visible_in_feed": boolish(r["is_visible_in_feed"]),
            "merged_into": r["merged_into"],
            "archived": boolish(r["archived"]),
            "prompt_version": r["prompt_version"],
            "representative_vector": vector_literal(r["representative_vector"]),
            "event_embedding": vector_literal(r["event_embedding"]),
            "created_run_id": r["created_run_id"],
            "last_touched_run_id": r["last_touched_run_id"],
            "published_run_id": r["published_run_id"],
            "published_at": pg_timestamp(r["published_at"]),
            "created_at": pg_timestamp(r["created_at"]),
        })
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_cluster_items(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.cluster_items (
          cluster_id, item_id, rank_in_cluster, added_at, is_primary_source,
          source_identity, join_decision_id
        )
        values (
          %(cluster_id)s, %(item_id)s, %(rank_in_cluster)s, %(added_at)s,
          %(is_primary_source)s, %(source_identity)s, %(join_decision_id)s
        )
        on conflict (cluster_id, item_id) do update set
          rank_in_cluster = excluded.rank_in_cluster,
          added_at = excluded.added_at,
          is_primary_source = excluded.is_primary_source,
          source_identity = excluded.source_identity,
          join_decision_id = excluded.join_decision_id
    """
    payload = [
        {
            "cluster_id": r["cluster_id"],
            "item_id": r["item_id"],
            "rank_in_cluster": r["rank_in_cluster"],
            "added_at": pg_timestamp(r["added_at"]),
            "is_primary_source": boolish(r["is_primary_source"]),
            "source_identity": r["source_identity"],
            "join_decision_id": r["join_decision_id"],
        }
        for r in rows
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_item_status(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.item_status (
          user_id, item_id, read_at, clicked_at, starred_at, hidden_at
        )
        values (
          %(user_id)s, %(item_id)s, %(read_at)s, %(clicked_at)s,
          %(starred_at)s, %(hidden_at)s
        )
        on conflict (user_id, item_id) do update set
          read_at = excluded.read_at,
          clicked_at = excluded.clicked_at,
          starred_at = excluded.starred_at,
          hidden_at = excluded.hidden_at
    """
    payload = [
        {
            "user_id": r["user_id"],
            "item_id": r["item_id"],
            "read_at": pg_timestamp(r["read_at"]),
            "clicked_at": pg_timestamp(r["clicked_at"]),
            "starred_at": pg_timestamp(r["starred_at"]),
            "hidden_at": pg_timestamp(r["hidden_at"]),
        }
        for r in rows
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_cluster_status(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.cluster_status (
          user_id, cluster_id, clicked_at, last_seen_version
        )
        values (
          %(user_id)s, %(cluster_id)s, %(clicked_at)s, %(last_seen_version)s
        )
        on conflict (user_id, cluster_id) do update set
          clicked_at = excluded.clicked_at,
          last_seen_version = excluded.last_seen_version
    """
    payload = [
        {
            "user_id": r["user_id"],
            "cluster_id": r["cluster_id"],
            "clicked_at": pg_timestamp(r["clicked_at"]),
            "last_seen_version": r["last_seen_version"],
        }
        for r in rows
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_fetch_runs(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.fetch_runs (id, started_at, finished_at, status, stats_json, error_msg)
        values (%(id)s, %(started_at)s, %(finished_at)s, %(status)s, %(stats_json)s::jsonb, %(error_msg)s)
        on conflict (id) do update set
          started_at = excluded.started_at,
          finished_at = excluded.finished_at,
          status = excluded.status,
          stats_json = excluded.stats_json,
          error_msg = excluded.error_msg
    """
    payload = [
        {
            "id": r["id"],
            "started_at": pg_timestamp(r["started_at"]),
            "finished_at": pg_timestamp(r["finished_at"]),
            "status": r["status"],
            "stats_json": jsonb(r["stats_json"]),
            "error_msg": r["error_msg"],
        }
        for r in rows
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def upsert_judge_logs(pg_conn, rows: Iterable[sqlite3.Row], schema: str) -> int:
    sql = f"""
        insert into {schema}.cluster_judge_log (
          id, item_id, candidate_cluster_ids, llm_input_tokens,
          llm_output_tokens, matches_json, selected_cluster_id,
          selection_reason, possible_merge_candidates, decision_model, created_at
        )
        values (
          %(id)s, %(item_id)s, %(candidate_cluster_ids)s::jsonb,
          %(llm_input_tokens)s, %(llm_output_tokens)s,
          %(matches_json)s::jsonb, %(selected_cluster_id)s,
          %(selection_reason)s, %(possible_merge_candidates)s::jsonb,
          %(decision_model)s, %(created_at)s
        )
        on conflict (id) do update set
          item_id = excluded.item_id,
          candidate_cluster_ids = excluded.candidate_cluster_ids,
          matches_json = excluded.matches_json,
          selected_cluster_id = excluded.selected_cluster_id,
          selection_reason = excluded.selection_reason,
          possible_merge_candidates = excluded.possible_merge_candidates,
          decision_model = excluded.decision_model,
          created_at = excluded.created_at
    """
    payload = [
        {
            "id": r["id"],
            "item_id": r["item_id"],
            "candidate_cluster_ids": jsonb(r["candidate_cluster_ids"]),
            "llm_input_tokens": r["llm_input_tokens"],
            "llm_output_tokens": r["llm_output_tokens"],
            "matches_json": jsonb(r["matches_json"]),
            "selected_cluster_id": r["selected_cluster_id"],
            "selection_reason": r["selection_reason"],
            "possible_merge_candidates": jsonb(r["possible_merge_candidates"]),
            "decision_model": r["decision_model"],
            "created_at": pg_timestamp(r["created_at"]),
        }
        for r in rows
    ]
    with pg_conn.cursor() as cur:
        cur.executemany(sql, payload)
    return len(payload)


def sync(args: argparse.Namespace) -> dict[str, int]:
    schema = checked_schema(args.schema)
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be greater than 0")
    if args.all and not args.confirm_full_sync:
        raise SystemExit(
            "--all requires --confirm-full-sync. Run --dry-run first and confirm "
            "remote capacity before pushing the full local database."
        )
    modes = [bool(args.all), bool(args.slim), bool(args.incremental)]
    if sum(1 for active in modes if active) > 1:
        raise SystemExit("--all, --slim, and --incremental are mutually exclusive.")
    if args.slim and not args.confirm_slim_sync:
        raise SystemExit(
            "--slim requires --confirm-slim-sync. Run --slim --dry-run first "
            "to inspect the selected scope."
        )
    if args.incremental and not args.confirm_incremental_sync:
        raise SystemExit(
            "--incremental requires --confirm-incremental-sync. Run --incremental --dry-run first "
            "to inspect the selected scope."
        )
    load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL is missing. Add it to .env and rerun.")
    started = time.time()
    sqlite_conn = sqlite_connect(args.db)
    try:
        pg_conn = pg_connect(db_url)
    except Exception as exc:
        sqlite_conn.close()
        raise SystemExit(f"Remote DB connection failed: {exc}") from exc
    try:
        slim_data = None
        incremental_data = None
        sample_data = None
        if args.all:
            local_plan = local_sync_plan(sqlite_conn, args.db, full=True)
        elif args.slim:
            slim_data = build_slim(
                sqlite_conn,
                days=args.slim_days,
                cluster_days=args.slim_cluster_days,
                recent_items_limit=args.slim_max_recent_items,
                judge_log_limit=args.slim_judge_log_limit,
            )
            local_plan = selected_sync_plan(
                sqlite_conn,
                args.db,
                slim_data,
                mode="slim",
                slim_days=args.slim_days,
                slim_cluster_days=args.slim_cluster_days,
                strip_heavy_fields=not args.slim_keep_heavy_fields,
            )
        elif args.incremental:
            incremental_data = build_incremental(
                sqlite_conn,
                hours=args.incremental_hours,
                recent_items_limit=args.incremental_max_recent_items,
                judge_log_limit=args.incremental_judge_log_limit,
            )
            local_plan = selected_sync_plan(
                sqlite_conn,
                args.db,
                incremental_data,
                mode="incremental",
                strip_heavy_fields=False,
            )
            local_plan["incremental"] = {
                "hours": args.incremental_hours,
                "max_recent_items": args.incremental_max_recent_items,
                "judge_log_limit": args.incremental_judge_log_limit,
                "strip_heavy_fields": False,
            }
        else:
            sample_data = build_sample(
                sqlite_conn,
                items_limit=args.items_limit,
                clusters_limit=args.clusters_limit,
            )
            local_plan = selected_sync_plan(
                sqlite_conn,
                args.db,
                sample_data,
                mode="sample",
            )
        if args.max_db_mib:
            assert_capacity_budget(
                local_plan,
                remote_db_size_bytes=pg_database_size_bytes(pg_conn),
                max_db_mib=args.max_db_mib,
                headroom_mib=args.capacity_headroom_mib,
            )
        if args.apply_schema:
            execute_schema(pg_conn, args.migration)
        counts: dict[str, int] = {}
        if args.all:
            if args.bulk_copy:
                counts["items"] = bulk_copy_all_items_by_id_batches(
                    sqlite_conn,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                    items_offset=args.items_offset,
                    items_limit=args.all_items_limit,
                )
            else:
                counts["items"] = upsert_all_items_by_id_batches(
                    sqlite_conn,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                    items_offset=args.items_offset,
                    items_limit=args.all_items_limit,
                )
            if args.only_items:
                return {
                    "items": counts["items"],
                    "clusters": 0,
                    "cluster_items": 0,
                    "item_status": 0,
                    "cluster_status": 0,
                    "fetch_runs": 0,
                    "cluster_judge_log": 0,
                }
            if args.bulk_copy:
                counts["clusters"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("clusters"),
                    pg_conn,
                    schema,
                    "clusters",
                    batch_size=args.batch_size,
                )
                counts["cluster_items"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_items"),
                    pg_conn,
                    schema,
                    "cluster_items",
                    batch_size=args.batch_size,
                )
                counts["item_status"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("item_status"),
                    pg_conn,
                    schema,
                    "item_status",
                    batch_size=args.batch_size,
                )
                counts["cluster_status"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_status"),
                    pg_conn,
                    schema,
                    "cluster_status",
                    batch_size=args.batch_size,
                )
                counts["fetch_runs"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("fetch_runs"),
                    pg_conn,
                    schema,
                    "fetch_runs",
                    batch_size=args.batch_size,
                )
                counts["cluster_judge_log"] = bulk_copy_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_judge_log"),
                    pg_conn,
                    schema,
                    "cluster_judge_log",
                    batch_size=args.batch_size,
                )
            else:
                counts["clusters"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("clusters"),
                    upsert_clusters,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
                counts["cluster_items"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_items"),
                    upsert_cluster_items,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
                counts["item_status"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("item_status"),
                    upsert_item_status,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
                counts["cluster_status"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_status"),
                    upsert_cluster_status,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
                counts["fetch_runs"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("fetch_runs"),
                    upsert_fetch_runs,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
                counts["cluster_judge_log"] = upsert_in_batches(
                    sqlite_conn,
                    all_table_select_sql("cluster_judge_log"),
                    upsert_judge_logs,
                    pg_conn,
                    schema,
                    batch_size=args.batch_size,
                )
        elif args.slim:
            assert slim_data is not None
            counts = sync_selected_rows(
                pg_conn,
                slim_data,
                schema,
                batch_size=args.batch_size,
                bulk_copy=args.bulk_copy,
                slim_items=not args.slim_keep_heavy_fields,
            )
        elif args.incremental:
            assert incremental_data is not None
            counts = sync_selected_rows(
                pg_conn,
                incremental_data,
                schema,
                batch_size=args.batch_size,
                bulk_copy=args.bulk_copy,
                slim_items=False,
            )
        else:
            assert sample_data is not None
            if args.bulk_copy:
                counts["items"] = bulk_copy_rows(pg_conn, sample_data["items"], schema, "items")
                counts["clusters"] = bulk_copy_rows(pg_conn, sample_data["clusters"], schema, "clusters")
                counts["cluster_items"] = bulk_copy_rows(
                    pg_conn, sample_data["cluster_items"], schema, "cluster_items"
                )
                counts["item_status"] = bulk_copy_rows(pg_conn, sample_data["item_status"], schema, "item_status")
                counts["cluster_status"] = bulk_copy_rows(
                    pg_conn, sample_data["cluster_status"], schema, "cluster_status"
                )
                counts["fetch_runs"] = bulk_copy_rows(pg_conn, sample_data["fetch_runs"], schema, "fetch_runs")
                counts["cluster_judge_log"] = bulk_copy_rows(
                    pg_conn, sample_data["cluster_judge_log"], schema, "cluster_judge_log"
                )
            else:
                counts["items"] = upsert_items(pg_conn, sample_data["items"], schema)
                counts["clusters"] = upsert_clusters(pg_conn, sample_data["clusters"], schema)
                counts["cluster_items"] = upsert_cluster_items(pg_conn, sample_data["cluster_items"], schema)
                counts["item_status"] = upsert_item_status(pg_conn, sample_data["item_status"], schema)
                counts["cluster_status"] = upsert_cluster_status(pg_conn, sample_data["cluster_status"], schema)
                counts["fetch_runs"] = upsert_fetch_runs(pg_conn, sample_data["fetch_runs"], schema)
                counts["cluster_judge_log"] = upsert_judge_logs(pg_conn, sample_data["cluster_judge_log"], schema)
        with pg_conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {schema}.sync_runs (
                  source_db_path, sample_name, items_attempted,
                  clusters_attempted, cluster_items_attempted,
                  item_status_attempted, cluster_status_attempted,
                  fetch_runs_attempted, judge_logs_attempted,
                  finished_at, stats_json
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s::jsonb)
                """,
                (
                    str(args.db),
                    args.sample_name,
                    counts["items"],
                    counts["clusters"],
                    counts["cluster_items"],
                    counts["item_status"],
                    counts["cluster_status"],
                    counts["fetch_runs"],
                    counts["cluster_judge_log"],
                    json.dumps({"elapsed_seconds": round(time.time() - started, 3), **counts}),
                ),
            )
        pg_conn.commit()
        return counts
    finally:
        sqlite_conn.close()
        pg_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--items-limit", type=int, default=200)
    parser.add_argument("--clusters-limit", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="Sync all supported tables in batches.")
    parser.add_argument(
        "--slim",
        action="store_true",
        help="Sync a free-tier friendly slice: recent items plus visible published event clusters.",
    )
    parser.add_argument("--slim-days", type=int, default=7)
    parser.add_argument("--slim-cluster-days", type=int, default=30)
    parser.add_argument("--slim-max-recent-items", type=int, default=25000)
    parser.add_argument("--slim-judge-log-limit", type=int, default=5000)
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Sync recently fetched items and recently updated clusters with full heavy fields.",
    )
    parser.add_argument("--incremental-hours", type=int, default=6)
    parser.add_argument("--incremental-max-recent-items", type=int, default=5000)
    parser.add_argument("--incremental-judge-log-limit", type=int, default=5000)
    parser.add_argument(
        "--items-offset",
        type=int,
        default=0,
        help="Recovery-only: in --all mode, skip the first N items in deterministic sync order.",
    )
    parser.add_argument(
        "--all-items-limit",
        type=int,
        default=None,
        help="Recovery-only: in --all mode, sync at most N items after --items-offset.",
    )
    parser.add_argument(
        "--only-items",
        action="store_true",
        help="Recovery-only: in --all mode, stop after syncing items.",
    )
    parser.add_argument(
        "--slim-keep-heavy-fields",
        action="store_true",
        help="Keep raw content/detail/comments in slim mode. Default strips them.",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--bulk-copy",
        action="store_true",
        help="Use PostgreSQL COPY into staging tables followed by server-side merge.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print local sync scope without connecting to Supabase.")
    parser.add_argument(
        "--confirm-full-sync",
        action="store_true",
        help="Required with --all to avoid accidental full remote imports.",
    )
    parser.add_argument(
        "--confirm-slim-sync",
        action="store_true",
        help="Required with --slim to avoid accidental medium-size remote imports.",
    )
    parser.add_argument(
        "--confirm-incremental-sync",
        action="store_true",
        help="Required with --incremental to avoid accidental remote writes.",
    )
    parser.add_argument(
        "--max-db-mib",
        type=float,
        default=None,
        help="Abort before remote writes when rough remote size after sync would exceed this MiB budget.",
    )
    parser.add_argument(
        "--capacity-headroom-mib",
        type=float,
        default=0,
        help="Extra MiB safety headroom reserved inside --max-db-mib.",
    )
    parser.add_argument("--schema", default="remote_poc")
    parser.add_argument("--sample-name", default="s0")
    parser.add_argument("--apply-schema", action="store_true")
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    args = parser.parse_args()

    if args.dry_run:
        conn = sqlite_connect(args.db)
        try:
            if args.slim:
                slim_data = build_slim(
                    conn,
                    days=args.slim_days,
                    cluster_days=args.slim_cluster_days,
                    recent_items_limit=args.slim_max_recent_items,
                    judge_log_limit=args.slim_judge_log_limit,
                )
                report = selected_sync_plan(
                    conn,
                    args.db,
                    slim_data,
                    mode="slim",
                    slim_days=args.slim_days,
                    slim_cluster_days=args.slim_cluster_days,
                    strip_heavy_fields=not args.slim_keep_heavy_fields,
                )
            elif args.incremental:
                incremental_data = build_incremental(
                    conn,
                    hours=args.incremental_hours,
                    recent_items_limit=args.incremental_max_recent_items,
                    judge_log_limit=args.incremental_judge_log_limit,
                )
                report = selected_sync_plan(
                    conn,
                    args.db,
                    incremental_data,
                    mode="incremental",
                    strip_heavy_fields=False,
                )
                report["incremental"] = {
                    "hours": args.incremental_hours,
                    "max_recent_items": args.incremental_max_recent_items,
                    "judge_log_limit": args.incremental_judge_log_limit,
                    "strip_heavy_fields": False,
                }
            else:
                report = local_sync_plan(conn, args.db, full=args.all)
        finally:
            conn.close()
        print(json.dumps({"dry_run": True, "local": report}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    counts = sync(args)
    print(json.dumps({"synced": counts}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
