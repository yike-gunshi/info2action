#!/usr/bin/env python3
"""Sync local SQLite-only app data into the remote Supabase schema.

This complements sync_sqlite_to_supabase_poc.py. The POC script is optimized
for feed/event payloads; this script covers the rest of the persistence surface
and preserves mixed-dimension vectors in auxiliary bytea/vector tables.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "feed.db"
DEFAULT_MIGRATION = ROOT / "supabase" / "migrations" / "0004_complete_sqlite_sync_support.sql"
SCHEMA_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

TABLE_ORDER = (
    "users",
    "invite_codes",
    "sessions",
    "user_profiles",
    "fetch_runs",
    "items",
    "clusters",
    "cluster_items",
    "fetch_run_items",
    "item_status",
    "cluster_status",
    "cluster_judge_log",
    "embedding_usage_logs",
    "search_keywords",
    "feedback",
    "briefings",
    "actions",
    "action_logs",
    "action_feedback",
    "interests",
    "interest_matches",
    "asr_usage",
    "settings",
    "health_log",
    "clusters_v2",
    "cluster_items_v2",
    "cluster_p_log",
)

EXCLUDED_COLUMNS = {
    # Heavy fields are synced by the feed/event sync script for new rows. This
    # complete pass updates metadata/app state without re-sending the whole DB.
    "items": {"content", "detail_json", "comments_json", "embedding"},
    "clusters": {"representative_vector", "event_embedding"},
    "clusters_v2": {"centroid"},
}

SQLITE_WHERE = {
    "sessions": "where user_id in (select id from users)",
    "user_profiles": "where user_id in (select id from users)",
    "invite_codes": "where created_by is null or created_by in (select id from users)",
    "cluster_items": "where cluster_id in (select id from clusters) and item_id in (select id from items)",
    "fetch_run_items": "where run_id in (select id from fetch_runs) and item_id in (select id from items)",
    "item_status": "where item_id in (select id from items)",
    "cluster_status": "where cluster_id in (select id from clusters)",
    "feedback": "where item_id in (select id from items)",
    "action_logs": "where action_id in (select id from actions)",
    "action_feedback": "where action_id in (select id from actions)",
    "interest_matches": "where item_id in (select id from items) and interest_id in (select id from interests)",
    "cluster_items_v2": "where cluster_id in (select id from clusters_v2) and item_id in (select id from items)",
}


@dataclass(frozen=True)
class PgColumn:
    name: str
    data_type: str
    udt_name: str


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key.strip()] = value


def require_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:  # pragma: no cover - depends on local env
        raise SystemExit(
            "Missing psycopg. Run with:\n"
            "  uv run --with 'psycopg[binary]>=3.2' --with numpy "
            "python scripts/sync_sqlite_to_supabase_complete.py --help"
        ) from exc
    return psycopg, dict_row


def ensure_read_write_session(pg_conn) -> None:
    pg_conn.execute("set default_transaction_read_only=off")
    pg_conn.commit()


def checked_schema(schema: str) -> str:
    if not SCHEMA_RE.match(schema):
        raise SystemExit(f"Unsafe schema name: {schema!r}")
    return schema


def checked_ident(name: str) -> str:
    if not SCHEMA_RE.match(name):
        raise SystemExit(f"Unsafe identifier: {name!r}")
    return name


def sqlite_connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"SQLite database not found: {path}")
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("select name from sqlite_master where type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row["name"]) for row in conn.execute(f"pragma table_info({checked_ident(table)})")]


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


def jsonb_value(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return json.dumps(clean_json_value(raw), ensure_ascii=False)
    if not isinstance(raw, str):
        return json.dumps(clean_json_value(raw), ensure_ascii=False)
    text = clean_text(raw) or ""
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = text
    return json.dumps(clean_json_value(parsed), ensure_ascii=False)


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


def boolish(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    return bool(value)


def vector_dim(blob: bytes | None) -> int | None:
    if blob is None:
        return None
    return int(len(blob) / 4)


def vector_literal(blob: bytes | None, dim: int) -> str | None:
    if blob is None or vector_dim(blob) != dim:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    return "[" + ",".join(f"{float(x):.8g}" for x in arr) + "]"


def is_doubao_embedding(provider: str | None, model: str | None, blob: bytes | None) -> bool:
    marker = f"{provider or ''} {model or ''}".lower()
    return "doubao" in marker or vector_dim(blob) == 2048


def row_value(row: sqlite3.Row, key: str) -> Any:
    value = row[key]
    return clean_text(value) if isinstance(value, str) else value


def convert_value(row: sqlite3.Row, column: PgColumn) -> Any:
    value = row_value(row, column.name)
    if column.udt_name == "jsonb":
        return jsonb_value(value)
    if column.data_type.startswith("timestamp"):
        return pg_timestamp(value)
    if column.data_type == "boolean":
        return boolish(value)
    if column.data_type in {"integer", "bigint"} and value == "":
        return None
    if column.data_type in {"double precision", "real", "numeric"} and value == "":
        return None
    if column.data_type == "text" and value is not None and not isinstance(value, str):
        return str(value)
    return value


def value_placeholder(column: PgColumn) -> str:
    if column.udt_name == "jsonb":
        return f"%({column.name})s::jsonb"
    return f"%({column.name})s"


def pg_columns(pg_conn, schema: str, table: str) -> list[PgColumn]:
    rows = pg_conn.execute(
        """
        select column_name, data_type, udt_name
          from information_schema.columns
         where table_schema = %s
           and table_name = %s
         order by ordinal_position
        """,
        (schema, table),
    ).fetchall()
    return [PgColumn(str(r["column_name"]), str(r["data_type"]), str(r["udt_name"])) for r in rows]


def primary_key_columns(pg_conn, schema: str, table: str) -> list[str]:
    rows = pg_conn.execute(
        """
        select a.attname
          from pg_index i
          join pg_class c on c.oid = i.indrelid
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)
         where n.nspname = %s
           and c.relname = %s
           and i.indisprimary
         order by array_position(i.indkey, a.attnum)
        """,
        (schema, table),
    ).fetchall()
    return [str(r["attname"]) for r in rows]


def selected_columns(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    table: str,
) -> list[PgColumn]:
    local = set(sqlite_columns(sqlite_conn, table))
    excluded = EXCLUDED_COLUMNS.get(table, set())
    return [
        col
        for col in pg_columns(pg_conn, schema, table)
        if col.name in local and col.name not in excluded and col.udt_name != "vector"
    ]


def upsert_sql(schema: str, table: str, columns: list[PgColumn], pk: list[str]) -> str:
    col_names = [col.name for col in columns]
    insert_cols = ", ".join(f'"{checked_ident(name)}"' for name in col_names)
    values = ", ".join(value_placeholder(col) for col in columns)
    conflict = ", ".join(f'"{checked_ident(name)}"' for name in pk)
    updates = [
        f'"{checked_ident(name)}" = excluded."{checked_ident(name)}"'
        for name in col_names
        if name not in pk
    ]
    update_sql = "nothing" if not updates else "update set " + ", ".join(updates)
    return (
        f'insert into {checked_schema(schema)}."{checked_ident(table)}" ({insert_cols}) '
        f"values ({values}) on conflict ({conflict}) do {update_sql}"
    )


def iter_sqlite_rows(conn: sqlite3.Connection, table: str, batch_size: int):
    where_sql = SQLITE_WHERE.get(table, "")
    cursor = conn.execute(f'select * from "{checked_ident(table)}" {where_sql}')
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        yield rows


def table_count(conn: sqlite3.Connection, table: str) -> int:
    where_sql = SQLITE_WHERE.get(table, "")
    return int(conn.execute(f'select count(*) as n from "{checked_ident(table)}" {where_sql}').fetchone()["n"])


def upsert_generic_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    table: str,
    *,
    batch_size: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    if table not in sqlite_tables(sqlite_conn):
        return 0
    columns = selected_columns(sqlite_conn, pg_conn, schema, table)
    pk = primary_key_columns(pg_conn, schema, table)
    if not columns or not pk:
        return 0
    if dry_run:
        return table_count(sqlite_conn, table)
    sql = upsert_sql(schema, table, columns, pk)
    total = 0
    for rows in iter_sqlite_rows(sqlite_conn, table, batch_size):
        payload = [{col.name: convert_value(row, col) for col in columns} for row in rows]
        with pg_conn.cursor() as cur:
            cur.executemany(sql, payload)
        pg_conn.commit()
        total += len(payload)
        if verbose:
            print(f"{table}: {total}", flush=True)
    return total


def sync_item_embedding_store(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
    offset: int = 0,
    dry_run: bool,
    verbose: bool,
) -> int:
    total_rows = int(
        sqlite_conn.execute(
            """
            select count(*) as n
              from items
             where embedding is not null
               and coalesce(embedding_provider, '') not like '%doubao%'
               and coalesce(embedding_model, '') not like '%doubao%'
               and length(embedding) != 8192
            """
        ).fetchone()["n"]
    )
    offset = max(0, int(offset))
    if dry_run:
        return max(0, total_rows - offset)
    sql = f"""
        insert into {checked_schema(schema)}.item_embedding_store (
          item_id, provider, model, input_variant, dim, embedding_float4,
          embedding_1024, embedding_1536, embedding_2048, generated_at, updated_at
        )
        values (
          %(item_id)s, %(provider)s, %(model)s, %(input_variant)s, %(dim)s,
          %(embedding_float4)s,
          %(embedding_1024)s::extensions.vector,
          %(embedding_1536)s::extensions.vector,
          %(embedding_2048)s::extensions.vector,
          %(generated_at)s, now()
        )
        on conflict (item_id) do update set
          provider = excluded.provider,
          model = excluded.model,
          input_variant = excluded.input_variant,
          dim = excluded.dim,
          embedding_float4 = excluded.embedding_float4,
          embedding_1024 = excluded.embedding_1024,
          embedding_1536 = excluded.embedding_1536,
          embedding_2048 = excluded.embedding_2048,
          generated_at = excluded.generated_at,
          updated_at = now()
    """
    cursor = sqlite_conn.execute(
        """
        select id, embedding, embedding_provider, embedding_model,
               embedding_input_variant, embedding_generated_at
          from items
         where embedding is not null
           and coalesce(embedding_provider, '') not like '%doubao%'
           and coalesce(embedding_model, '') not like '%doubao%'
           and length(embedding) != 8192
         order by id
         limit -1 offset ?
        """
        ,
        (offset,),
    )
    total = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        payload = []
        for row in rows:
            blob = row["embedding"]
            if is_doubao_embedding(row["embedding_provider"], row["embedding_model"], blob):
                continue
            payload.append({
                "item_id": row["id"],
                "provider": row["embedding_provider"],
                "model": row["embedding_model"],
                "input_variant": row["embedding_input_variant"],
                "dim": vector_dim(blob),
                "embedding_float4": blob,
                "embedding_1024": vector_literal(blob, 1024),
                "embedding_1536": vector_literal(blob, 1536),
                "embedding_2048": vector_literal(blob, 2048),
                "generated_at": pg_timestamp(row["embedding_generated_at"]),
            })
        if payload:
            with pg_conn.cursor() as cur:
                cur.executemany(sql, payload)
            pg_conn.commit()
            total += len(payload)
            if verbose:
                print(f"item_embedding_store: {total}", flush=True)
    return total


def sync_cluster_vector_store(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    total_rows = int(sqlite_conn.execute(
        """
        select count(*) as n
          from (
            select id from clusters where representative_vector is not null and length(representative_vector) != 8192
            union all
            select id from clusters where event_embedding is not null and length(event_embedding) != 8192
          )
        """
    ).fetchone()["n"])
    if dry_run:
        return total_rows
    sql = f"""
        insert into {checked_schema(schema)}.cluster_vector_store (
          cluster_id, vector_kind, dim, vector_float4,
          vector_1024, vector_1536, vector_2048, updated_at
        )
        values (
          %(cluster_id)s, %(vector_kind)s, %(dim)s, %(vector_float4)s,
          %(vector_1024)s::extensions.vector,
          %(vector_1536)s::extensions.vector,
          %(vector_2048)s::extensions.vector,
          now()
        )
        on conflict (cluster_id, vector_kind) do update set
          dim = excluded.dim,
          vector_float4 = excluded.vector_float4,
          vector_1024 = excluded.vector_1024,
          vector_1536 = excluded.vector_1536,
          vector_2048 = excluded.vector_2048,
          updated_at = now()
    """
    cursor = sqlite_conn.execute(
        """
        select id, representative_vector, event_embedding
          from clusters
         where representative_vector is not null or event_embedding is not null
         order by id
        """
    )
    total = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        payload = []
        for row in rows:
            for kind, key in (("representative", "representative_vector"), ("event", "event_embedding")):
                blob = row[key]
                if blob is None:
                    continue
                if vector_dim(blob) == 2048:
                    continue
                payload.append({
                    "cluster_id": row["id"],
                    "vector_kind": kind,
                    "dim": vector_dim(blob),
                    "vector_float4": blob,
                    "vector_1024": vector_literal(blob, 1024),
                    "vector_1536": vector_literal(blob, 1536),
                    "vector_2048": vector_literal(blob, 2048),
                })
        if payload:
            with pg_conn.cursor() as cur:
                cur.executemany(sql, payload)
            pg_conn.commit()
            total += len(payload)
            if verbose:
                print(f"cluster_vector_store: {total}", flush=True)
    return total


def sync_cluster_v2_vector_store(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    schema: str,
    *,
    batch_size: int,
    dry_run: bool,
    verbose: bool,
) -> int:
    if "clusters_v2" not in sqlite_tables(sqlite_conn):
        return 0
    total_rows = int(
        sqlite_conn.execute("select count(*) as n from clusters_v2 where centroid is not null").fetchone()["n"]
    )
    if dry_run:
        return total_rows
    sql = f"""
        insert into {checked_schema(schema)}.cluster_v2_vector_store (
          cluster_id, dim, vector_float4, vector_1024, vector_1536, vector_2048, updated_at
        )
        values (
          %(cluster_id)s, %(dim)s, %(vector_float4)s,
          %(vector_1024)s::extensions.vector,
          %(vector_1536)s::extensions.vector,
          %(vector_2048)s::extensions.vector,
          now()
        )
        on conflict (cluster_id) do update set
          dim = excluded.dim,
          vector_float4 = excluded.vector_float4,
          vector_1024 = excluded.vector_1024,
          vector_1536 = excluded.vector_1536,
          vector_2048 = excluded.vector_2048,
          updated_at = now()
    """
    cursor = sqlite_conn.execute("select id, centroid from clusters_v2 where centroid is not null order by id")
    total = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        payload = []
        for row in rows:
            blob = row["centroid"]
            payload.append({
                "cluster_id": row["id"],
                "dim": vector_dim(blob),
                "vector_float4": blob,
                "vector_1024": vector_literal(blob, 1024),
                "vector_1536": vector_literal(blob, 1536),
                "vector_2048": vector_literal(blob, 2048),
            })
        with pg_conn.cursor() as cur:
            cur.executemany(sql, payload)
        pg_conn.commit()
        total += len(payload)
        if verbose:
            print(f"cluster_v2_vector_store: {total}", flush=True)
    return total


def reset_sequences(pg_conn, schema: str, tables: Iterable[str]) -> None:
    with pg_conn.cursor() as cur:
        for table in tables:
            pk = primary_key_columns(pg_conn, schema, table)
            if pk != ["id"]:
                continue
            seq_row = cur.execute(
                "select pg_get_serial_sequence(%s, 'id') as seq",
                (f"{schema}.{table}",),
            ).fetchone()
            seq = seq_row["seq"] if seq_row else None
            if not seq:
                continue
            cur.execute(
                f"select setval(%s, greatest(coalesce((select max(id) from {schema}.{checked_ident(table)}), 0), 1), true)",
                (seq,),
            )
    pg_conn.commit()


def refresh_main_vector_columns(pg_conn, schema: str, *, dry_run: bool) -> dict[str, int]:
    if dry_run:
        return {
            "items_main_embedding_refreshed": 0,
            "clusters_main_representative_vector_refreshed": 0,
            "clusters_main_event_embedding_refreshed": 0,
        }
    checked_schema(schema)
    with pg_conn.cursor() as cur:
        cur.execute(
            f"""
            update {schema}.items i
               set embedding = s.embedding_1536,
                   embedding_provider = coalesce(i.embedding_provider, s.provider),
                   embedding_model = coalesce(i.embedding_model, s.model),
                   embedding_input_variant = coalesce(i.embedding_input_variant, s.input_variant),
                   embedding_generated_at = coalesce(i.embedding_generated_at, s.generated_at)
              from {schema}.item_embedding_store s
             where s.item_id = i.id
               and s.dim = 1536
               and s.embedding_1536 is not null
               and i.embedding is null
            """
        )
        item_count = cur.rowcount
        cur.execute(
            f"""
            update {schema}.clusters c
               set representative_vector = s.vector_1536
              from {schema}.cluster_vector_store s
             where s.cluster_id = c.id
               and s.vector_kind = 'representative'
               and s.dim = 1536
               and s.vector_1536 is not null
               and c.representative_vector is null
            """
        )
        cluster_count = cur.rowcount
        cur.execute(
            f"""
            update {schema}.clusters c
               set event_embedding = s.vector_1536
              from {schema}.cluster_vector_store s
             where s.cluster_id = c.id
               and s.vector_kind = 'event'
               and s.dim = 1536
               and s.vector_1536 is not null
               and c.event_embedding is null
            """
        )
        event_count = cur.rowcount
    pg_conn.commit()
    return {
        "items_main_embedding_refreshed": int(item_count),
        "clusters_main_representative_vector_refreshed": int(cluster_count),
        "clusters_main_event_embedding_refreshed": int(event_count),
    }


def apply_schema(pg_conn, migration: Path) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(migration.read_text(encoding="utf-8"))
    pg_conn.commit()


def sync(args: argparse.Namespace) -> dict[str, int]:
    checked_schema(args.schema)
    load_env_file(args.env_file)
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL is missing")
    psycopg, dict_row = require_psycopg()
    sqlite_conn = sqlite_connect(args.db)
    try:
        with psycopg.connect(db_url, row_factory=dict_row, connect_timeout=30) as pg_conn:
            ensure_read_write_session(pg_conn)
            if args.apply_schema and not args.dry_run:
                apply_schema(pg_conn, args.migration)
            counts: dict[str, int] = {}
            skipping = bool(args.skip_through)
            for table in TABLE_ORDER:
                if skipping:
                    counts[table] = 0
                    if table == args.skip_through:
                        skipping = False
                    continue
                counts[table] = upsert_generic_table(
                    sqlite_conn,
                    pg_conn,
                    args.schema,
                    table,
                    batch_size=args.batch_size,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                )
            counts["item_embedding_store"] = sync_item_embedding_store(
                sqlite_conn,
                pg_conn,
                args.schema,
                batch_size=args.batch_size,
                offset=args.item_embedding_offset,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            counts["cluster_vector_store"] = sync_cluster_vector_store(
                sqlite_conn,
                pg_conn,
                args.schema,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            counts["cluster_v2_vector_store"] = sync_cluster_v2_vector_store(
                sqlite_conn,
                pg_conn,
                args.schema,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                verbose=args.verbose,
            )
            counts.update(refresh_main_vector_columns(pg_conn, args.schema, dry_run=args.dry_run))
            if not args.dry_run:
                reset_sequences(pg_conn, args.schema, TABLE_ORDER)
            return counts
    finally:
        sqlite_conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--schema", default="remote_poc")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--item-embedding-offset",
        type=int,
        default=0,
        help="Resume helper for item_embedding_store ordered by item id.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-schema", action="store_true")
    parser.add_argument("--migration", type=Path, default=DEFAULT_MIGRATION)
    parser.add_argument(
        "--skip-through",
        choices=TABLE_ORDER,
        help="Resume helper: skip ordered generic tables through and including this table.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    started = time.time()
    counts = sync(args)
    print(json.dumps({
        "dry_run": args.dry_run,
        "elapsed_seconds": round(time.time() - started, 3),
        "synced": counts,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
