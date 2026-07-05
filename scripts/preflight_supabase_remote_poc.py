#!/usr/bin/env python3
"""Preflight the local SQLite -> Supabase POC cutover.

This report is intentionally credential-free: it prints counts, sizes, vector
health, and rough capacity signals without echoing connection strings.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from sync_sqlite_to_supabase_poc import (
    DEFAULT_DB,
    SYNC_TABLES,
    build_slim,
    checked_schema,
    load_dotenv,
    local_sync_plan,
    pg_connect,
    selected_sync_plan,
    sqlite_connect,
)


IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(identifier: str) -> str:
    if not IDENT_RE.match(identifier):
        raise ValueError(f"Unsafe identifier: {identifier!r}")
    return f'"{identifier}"'


def local_column_sizes(sqlite_conn, table: str, *, top: int = 12) -> list[dict[str, Any]]:
    qtable = quote_ident(table)
    columns = [
        row["name"]
        for row in sqlite_conn.execute(f"PRAGMA table_info({qtable})").fetchall()
        if IDENT_RE.match(row["name"])
    ]
    sizes: list[dict[str, Any]] = []
    for col in columns:
        qcol = quote_ident(col)
        row = sqlite_conn.execute(
            f"SELECT sum(length({qcol})) AS bytes FROM {qtable} WHERE {qcol} IS NOT NULL"
        ).fetchone()
        size = int(row["bytes"] or 0)
        if size > 0:
            sizes.append({"column": col, "bytes": size, "mib": mib(size)})
    sizes.sort(key=lambda item: item["bytes"], reverse=True)
    return sizes[:top]


def remote_report(pg_conn, schema: str) -> dict[str, Any]:
    with pg_conn.cursor() as cur:
        version = cur.execute("SELECT version() AS version").fetchone()["version"]
        vector_extension = cur.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone() is not None
        db_size = cur.execute(
            "SELECT pg_database_size(current_database()) AS n"
        ).fetchone()["n"]
        schema_exists = cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (schema,),
        ).fetchone() is not None
        table_counts: dict[str, int | None] = {}
        table_sizes: dict[str, int | None] = {}
        for table in SYNC_TABLES:
            regclass = cur.execute(
                "SELECT to_regclass(%s) AS regclass",
                (f"{schema}.{table}",),
            ).fetchone()["regclass"]
            if not regclass:
                table_counts[table] = None
                table_sizes[table] = None
                continue
            table_counts[table] = int(
                cur.execute(f"SELECT count(*) AS n FROM {schema}.{table}").fetchone()["n"]
            )
            table_sizes[table] = int(
                cur.execute("SELECT pg_total_relation_size(%s::regclass) AS n", (regclass,)).fetchone()["n"]
            )
    return {
        "postgres_version": version.split(" on ")[0],
        "vector_extension": vector_extension,
        "db_size_bytes": int(db_size),
        "schema_exists": schema_exists,
        "tables": table_counts,
        "table_size_bytes": table_sizes,
    }


def mib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1024 / 1024, 2)


def add_human_sizes(report: dict[str, Any]) -> dict[str, Any]:
    local = report.get("local", {})
    remote = report.get("remote", {})
    local["db_size_mib"] = mib(local.get("db_size_bytes"))
    remote["db_size_mib"] = mib(remote.get("db_size_bytes"))
    if isinstance(remote.get("table_size_bytes"), dict):
        remote["table_size_mib"] = {
            table: mib(size)
            for table, size in remote["table_size_bytes"].items()
        }
    return report


def capacity_check(report: dict[str, Any], *, max_db_mib: float | None) -> dict[str, Any]:
    local_bytes = (
        report["local"].get("estimated_payload_bytes")
        or report["local"].get("db_size_bytes")
        or 0
    )
    remote_bytes = report["remote"].get("db_size_bytes") or 0
    rough_after = local_bytes + remote_bytes
    out: dict[str, Any] = {
        "rough_after_sync_bytes": rough_after,
        "rough_after_sync_mib": mib(rough_after),
        "local_bytes_basis": (
            "estimated_payload_bytes"
            if report["local"].get("estimated_payload_bytes") is not None
            else "sqlite_file_size"
        ),
        "note": "Rough estimate uses selected payload bytes when available, otherwise local SQLite file size; Postgres storage may differ.",
    }
    if max_db_mib and max_db_mib > 0:
        max_bytes = int(max_db_mib * 1024 * 1024)
        out["max_db_mib"] = max_db_mib
        out["would_exceed_max"] = rough_after > max_bytes
    else:
        out["max_db_mib"] = None
        out["would_exceed_max"] = None
    return out


def risks(report: dict[str, Any]) -> list[str]:
    found: list[str] = []
    vectors = report["local"].get("vectors", {})
    refs = report["local"].get("referential_checks", {})
    if vectors.get("bad_item_embedding_dimensions"):
        found.append("local items contain embeddings with unexpected dimensions")
    if vectors.get("bad_cluster_vector_dimensions"):
        found.append("local clusters contain representative vectors with unexpected dimensions")
    if refs.get("cluster_items_missing_cluster"):
        found.append("local cluster_items has rows with missing cluster references")
    if refs.get("cluster_items_missing_item"):
        found.append("local cluster_items has rows with missing item references")
    remote = report.get("remote", {})
    if not remote.get("vector_extension"):
        found.append("remote pgvector extension is not enabled")
    if not remote.get("schema_exists"):
        found.append("remote schema does not exist; run sync with --apply-schema first")
    cap = report.get("capacity", {})
    if cap.get("would_exceed_max"):
        found.append("rough sync estimate exceeds configured max DB size")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", default="remote_poc")
    parser.add_argument("--slim", action="store_true", help="Preflight the slim sync profile instead of full sync.")
    parser.add_argument("--slim-days", type=int, default=7)
    parser.add_argument("--slim-cluster-days", type=int, default=30)
    parser.add_argument("--slim-max-recent-items", type=int, default=25000)
    parser.add_argument("--slim-judge-log-limit", type=int, default=5000)
    parser.add_argument("--slim-keep-heavy-fields", action="store_true")
    parser.add_argument(
        "--max-db-mib",
        type=float,
        default=None,
        help="Optional capacity threshold for the rough after-full-sync estimate.",
    )
    parser.add_argument(
        "--fail-on-risk",
        action="store_true",
        help="Return exit code 2 when any risk is detected.",
    )
    args = parser.parse_args()

    schema = checked_schema(args.schema)
    load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL is missing. Add it to .env and rerun.")

    sqlite_conn = sqlite_connect(args.db)
    try:
        pg_conn = pg_connect(db_url)
    except Exception as exc:
        raise SystemExit(f"Remote DB connection failed: {exc}") from exc
    try:
        if args.slim:
            slim_data = build_slim(
                sqlite_conn,
                days=args.slim_days,
                cluster_days=args.slim_cluster_days,
                recent_items_limit=args.slim_max_recent_items,
                judge_log_limit=args.slim_judge_log_limit,
            )
            local = selected_sync_plan(
                sqlite_conn,
                args.db,
                slim_data,
                mode="slim",
                slim_days=args.slim_days,
                slim_cluster_days=args.slim_cluster_days,
                strip_heavy_fields=not args.slim_keep_heavy_fields,
            )
        else:
            local = local_sync_plan(sqlite_conn, args.db, full=True)
        report: dict[str, Any] = {
            "local": local,
            "local_column_sizes": {
                "items": local_column_sizes(sqlite_conn, "items"),
                "clusters": local_column_sizes(sqlite_conn, "clusters"),
            },
            "remote": remote_report(pg_conn, schema),
        }
    finally:
        sqlite_conn.close()
        pg_conn.close()

    report["capacity"] = capacity_check(report, max_db_mib=args.max_db_mib)
    report["risks"] = risks(report)
    add_human_sizes(report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_risk and report["risks"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
