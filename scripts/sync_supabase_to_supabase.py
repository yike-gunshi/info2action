#!/usr/bin/env python3
"""Mirror one Supabase Postgres schema into another Supabase project.

This is intended for production -> staging refreshes during the remote-only
cutover. The source is opened read-only. The target is expected to be a staging
project and can be truncated before import.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - exercised by CLI guard tests
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = "remote_poc"

KNOWN_TABLE_ORDER = (
    "fetch_runs",
    "items",
    "clusters",
    "cluster_items",
    "fetch_run_items",
    "item_status",
    "cluster_status",
    "cluster_judge_log",
    "sync_runs",
    "search_keywords",
    "feedback",
    "briefings",
    "actions",
    "action_logs",
    "action_feedback",
    "interests",
    "interest_matches",
    "health_log",
    "users",
    "invite_codes",
    "sessions",
    "user_profiles",
    "asr_usage",
    "settings",
    "clusters_v2",
    "cluster_items_v2",
    "cluster_p_log",
    "remote_assets",
    "item_feedback",
    "system_feedback",
    "preference_signals",
)

CUSTOM_SEQUENCE_TABLES = {
    "fetch_runs": "fetch_runs_id_seq",
    "clusters": "clusters_id_seq",
    "cluster_judge_log": "cluster_judge_log_id_seq",
}


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"env file not found: {path}") from exc

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def checked_ident(name: str, label: str) -> str:
    if not name.replace("_", "").isalnum() or not (name[0].isalpha() or name[0] == "_"):
        raise SystemExit(f"invalid {label}: {name!r}")
    return name


def db_url_from_env(values: dict[str, str], *, prefer_direct: bool) -> str:
    keys = (
        ("SUPABASE_DB_DIRECT_URL", "SUPABASE_DB_URL", "DATABASE_URL")
        if prefer_direct
        else ("SUPABASE_DB_URL", "SUPABASE_DB_DIRECT_URL", "DATABASE_URL")
    )
    for key in keys:
        value = values.get(key)
        if value:
            return value
    raise SystemExit("env file is missing SUPABASE_DB_URL")


def redact_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "unknown"
    user = parsed.username or "unknown"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{user}:***@{host}{port}{parsed.path}"


def require_psycopg() -> Any:
    if psycopg is None or sql is None:
        raise SystemExit(
            "Missing psycopg. Run with:\n"
            "  uv run --with 'psycopg[binary]>=3.2,<4.0' "
            "python scripts/sync_supabase_to_supabase.py --help"
        )
    return psycopg


def connect(
    url: str,
    *,
    application_name: str,
    readonly: bool = False,
    retries: int = 3,
) -> psycopg.Connection:
    pg = require_psycopg()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            conn = pg.connect(url, connect_timeout=30, application_name=application_name)
            conn.execute("set statement_timeout = '30min'")
            conn.execute("set lock_timeout = '30s'")
            if readonly:
                conn.execute("set default_transaction_read_only = on")
            return conn
        except pg.OperationalError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            emit(
                {
                    "event": "connect_retry",
                    "application_name": application_name,
                    "attempt": attempt,
                    "retries": retries,
                    "error": type(exc).__name__,
                },
                stderr=True,
            )
            time.sleep(min(10, 2 * attempt))
    assert last_exc is not None
    raise last_exc


def schema_tables(conn: psycopg.Connection, schema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select tablename
              from pg_tables
             where schemaname = %s
             order by tablename
            """,
            (schema,),
        )
        return {str(row[0]) for row in cur.fetchall()}


def ordered_tables(source: psycopg.Connection, target: psycopg.Connection, schema: str) -> list[str]:
    source_tables = schema_tables(source, schema)
    target_tables = schema_tables(target, schema)
    common = source_tables & target_tables
    missing_in_target = sorted(source_tables - target_tables)
    missing_in_source = sorted(target_tables - source_tables)
    if missing_in_target:
        raise SystemExit(f"target is missing source tables: {missing_in_target}")
    ordered = [table for table in KNOWN_TABLE_ORDER if table in common]
    extras = sorted(common - set(ordered))
    if extras:
        ordered.extend(extras)
    if missing_in_source:
        print(
            json.dumps(
                {"warning": "target has tables absent from source", "tables": missing_in_source},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return ordered


def table_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select column_name
              from information_schema.columns
             where table_schema = %s
               and table_name = %s
               and is_generated = 'NEVER'
             order by ordinal_position
            """,
            (schema, table),
        )
        return [str(row[0]) for row in cur.fetchall()]


def copy_columns(
    source: psycopg.Connection,
    target: psycopg.Connection,
    schema: str,
    table: str,
) -> list[str]:
    source_columns = table_columns(source, schema, table)
    target_columns = table_columns(target, schema, table)
    target_set = set(target_columns)
    columns = [column for column in source_columns if column in target_set]
    if not columns:
        raise SystemExit(f"no common columns for {schema}.{table}")
    missing_in_target = [column for column in source_columns if column not in target_set]
    if missing_in_target:
        print(
            json.dumps(
                {
                    "warning": "source columns skipped because target lacks them",
                    "table": table,
                    "columns": missing_in_target,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return columns


def count_rows(conn: psycopg.Connection, schema: str, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("select count(*) from {}.{}").format(
                sql.Identifier(schema),
                sql.Identifier(table),
            )
        )
        return int(cur.fetchone()[0])


def snapshot(conn: psycopg.Connection, schema: str, tables: list[str]) -> dict[str, Any]:
    counts = {table: count_rows(conn, schema, table) for table in tables}
    with conn.cursor() as cur:
        cur.execute("select pg_size_pretty(pg_database_size(current_database()))")
        database_size = str(cur.fetchone()[0])
        cur.execute(
            """
            select coalesce(
                     pg_size_pretty(
                       sum(pg_total_relation_size(
                         (quote_ident(schemaname) || '.' || quote_ident(tablename))::regclass
                       ))
                     ),
                     '0 bytes'
                   )
              from pg_tables
             where schemaname = %s
            """,
            (schema,),
        )
        schema_size = str(cur.fetchone()[0])
        cur.execute("select count(*) from storage.objects where bucket_id = 'info2action-assets'")
        storage_objects = int(cur.fetchone()[0])
    return {
        "counts": counts,
        "database_size": database_size,
        "schema_size": schema_size,
        "storage_objects": storage_objects,
    }


def truncate_target(conn: psycopg.Connection, schema: str, tables: list[str]) -> None:
    if not tables:
        return
    table_expr = sql.SQL(", ").join(
        sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        for table in tables
    )
    with conn.cursor() as cur:
        cur.execute(sql.SQL("truncate table {} restart identity cascade").format(table_expr))
    conn.commit()


def copy_table(
    source: psycopg.Connection,
    target: psycopg.Connection,
    schema: str,
    table: str,
    *,
    dry_run: bool,
    chunk_size: int,
    copy_format: str,
) -> dict[str, Any]:
    columns = copy_columns(source, target, schema, table)
    source_count = count_rows(source, schema, table)
    result: dict[str, Any] = {
        "table": table,
        "columns": len(columns),
        "copy_format": copy_format,
        "source_count": source_count,
    }
    if dry_run:
        result["target_count"] = count_rows(target, schema, table)
        result["copied"] = False
        return result

    start = time.monotonic()
    col_expr = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
    copy_options = (
        sql.SQL("with (format binary)")
        if copy_format == "binary"
        else sql.SQL("with (format csv, null '\\N')")
    )

    def stream_copy(where_clause: sql.Composable, *, order_by_id: bool) -> None:
        order_clause = (
            sql.SQL(" order by {}").format(sql.Identifier("id"))
            if order_by_id
            else sql.SQL("")
        )
        copy_out = sql.SQL(
            "copy (select {} from {}.{}{}{}) to stdout {}"
        ).format(
            col_expr,
            sql.Identifier(schema),
            sql.Identifier(table),
            where_clause,
            order_clause,
            copy_options,
        )
        copy_in = sql.SQL(
            "copy {}.{} ({}) from stdin {}"
        ).format(sql.Identifier(schema), sql.Identifier(table), col_expr, copy_options)
        with source.cursor() as source_cur, target.cursor() as target_cur:
            with source_cur.copy(copy_out) as reader:
                with target_cur.copy(copy_in) as writer:
                    while True:
                        data = reader.read()
                        if not data:
                            break
                        writer.write(data)

    try:
        if "id" in columns and chunk_size > 0 and source_count > chunk_size:
            copied = 0
            chunk_index = 0
            last_id: Any | None = None
            while True:
                if last_id is None:
                    where_for_keys = sql.SQL("")
                else:
                    where_for_keys = sql.SQL(" where {} > {}").format(
                        sql.Identifier("id"),
                        sql.Literal(last_id),
                    )
                key_sql = sql.SQL(
                    "select {} from {}.{}{} order by {} limit {}"
                ).format(
                    sql.Identifier("id"),
                    sql.Identifier(schema),
                    sql.Identifier(table),
                    where_for_keys,
                    sql.Identifier("id"),
                    sql.Literal(chunk_size),
                )
                with source.cursor() as cur:
                    cur.execute(key_sql)
                    ids = [row[0] for row in cur.fetchall()]
                if not ids:
                    break
                upper_id = ids[-1]
                if last_id is None:
                    where_for_copy = sql.SQL(" where {} <= {}").format(
                        sql.Identifier("id"),
                        sql.Literal(upper_id),
                    )
                else:
                    where_for_copy = sql.SQL(" where {} > {} and {} <= {}").format(
                        sql.Identifier("id"),
                        sql.Literal(last_id),
                        sql.Identifier("id"),
                        sql.Literal(upper_id),
                    )
                stream_copy(where_for_copy, order_by_id=True)
                target.commit()
                copied += len(ids)
                chunk_index += 1
                emit(
                    {
                        "chunk_result": {
                            "table": table,
                            "chunk": chunk_index,
                            "copied_estimate": copied,
                            "source_count": source_count,
                            "upper_id": str(upper_id),
                        }
                    }
                )
                last_id = upper_id
        else:
            stream_copy(sql.SQL(""), order_by_id=False)
            target.commit()
    except Exception:
        target.rollback()
        raise

    target_count = count_rows(target, schema, table)
    elapsed = time.monotonic() - start
    result.update(
        {
            "target_count": target_count,
            "copied": True,
            "elapsed_sec": round(elapsed, 2),
            "ok": target_count == source_count,
        }
    )
    return result


def reset_sequences(conn: psycopg.Connection, schema: str, tables: list[str]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for table in tables:
            columns = table_columns(conn, schema, table)
            if "id" not in columns:
                continue
            cur.execute("select pg_get_serial_sequence(%s, 'id')", (f"{schema}.{table}",))
            sequence = cur.fetchone()[0]
            custom = CUSTOM_SEQUENCE_TABLES.get(table)
            if not sequence and custom:
                sequence = f"{schema}.{custom}"
            if not sequence:
                continue
            cur.execute(
                sql.SQL("select coalesce(max(id), 0) from {}.{}").format(
                    sql.Identifier(schema),
                    sql.Identifier(table),
                )
            )
            max_id = cur.fetchone()[0]
            if max_id is None:
                continue
            if not isinstance(max_id, int):
                continue
            if max_id <= 0:
                cur.execute("select setval(%s::regclass, 1, false)", (sequence,))
                value = 1
                is_called = False
            else:
                cur.execute("select setval(%s::regclass, %s, true)", (sequence, max_id))
                value = max_id
                is_called = True
            reports.append(
                {
                    "table": table,
                    "sequence": str(sequence),
                    "value": int(value),
                    "is_called": is_called,
                }
            )
    conn.commit()
    return reports


def emit(payload: dict[str, Any], *, stderr: bool = False) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr if stderr else sys.stdout, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--target-env", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--schema", default=os.environ.get("SUPABASE_REMOTE_DB_SCHEMA", DEFAULT_SCHEMA))
    parser.add_argument("--prefer-direct", action="store_true")
    parser.add_argument("--truncate-target", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--chunk-size", type=int, default=2000)
    parser.add_argument("--copy-format", choices=("binary", "csv"), default="binary")
    parser.add_argument("--yes", action="store_true", help="confirm destructive staging refresh")
    parser.add_argument(
        "--allow-non-staging-target",
        action="store_true",
        help="disable the guard that requires SUPABASE_REMOTE_DB_ENV=staging on target",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    schema = checked_ident(args.schema, "schema")
    source_env = load_env_file(args.source_env)
    target_env = load_env_file(args.target_env)
    source_url = db_url_from_env(source_env, prefer_direct=args.prefer_direct)
    target_url = db_url_from_env(target_env, prefer_direct=args.prefer_direct)

    if source_url == target_url:
        raise SystemExit("source and target database URLs are identical")
    target_name = target_env.get("SUPABASE_REMOTE_DB_ENV", "")
    if target_name != "staging" and not args.allow_non_staging_target:
        raise SystemExit(
            "target env must set SUPABASE_REMOTE_DB_ENV=staging "
            "(or pass --allow-non-staging-target)"
        )
    if args.truncate_target and not args.yes:
        raise SystemExit("--truncate-target requires --yes")

    emit(
        {
            "source": redact_url(source_url),
            "target": redact_url(target_url),
            "schema": schema,
            "dry_run": args.dry_run,
            "truncate_target": args.truncate_target,
        }
    )

    with connect(source_url, application_name="info2action_prod_to_staging_source", readonly=True) as source:
        with connect(target_url, application_name="info2action_prod_to_staging_target") as target:
            tables = ordered_tables(source, target, schema)
            before = {
                "source": snapshot(source, schema, tables),
                "target": snapshot(target, schema, tables),
            }
            emit({"before": before})
            if args.truncate_target and not args.dry_run:
                emit({"event": "truncate_target", "tables": len(tables)})
                truncate_target(target, schema, tables)

            table_reports = []
            for table in tables:
                report = copy_table(
                    source,
                    target,
                    schema,
                    table,
                    dry_run=args.dry_run,
                    chunk_size=args.chunk_size,
                    copy_format=args.copy_format,
                )
                table_reports.append(report)
                emit({"table_result": report})

            sequence_reports: list[dict[str, Any]] = []
            if not args.dry_run:
                sequence_reports = reset_sequences(target, schema, tables)
            after = {
                "source": snapshot(source, schema, tables),
                "target": snapshot(target, schema, tables),
            }

    mismatches = {
        table: {
            "source": after["source"]["counts"][table],
            "target": after["target"]["counts"][table],
        }
        for table in after["source"]["counts"]
        if after["source"]["counts"][table] != after["target"]["counts"][table]
    }
    final_report = {
        "after": after,
        "dry_run": args.dry_run,
        "mismatches": mismatches,
        "ok": not mismatches,
        "sequence_resets": sequence_reports,
        "tables": len(table_reports),
    }
    emit(final_report)
    return 0 if args.dry_run or not mismatches else 2


if __name__ == "__main__":
    raise SystemExit(main())
