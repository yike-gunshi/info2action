#!/usr/bin/env python3
"""Verify the Supabase remote DB POC without exposing credentials."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from export_remote_db_sample import placeholders
from sync_sqlite_to_supabase_poc import (
    ROOT,
    VECTOR_DIM,
    VECTOR_SQL_TYPE,
    checked_schema,
    load_dotenv,
    pg_connect,
    sqlite_connect,
    vector_literal,
)


DEFAULT_DB = ROOT / "data" / "feed.db"


def table_counts(pg_conn, schema: str) -> dict[str, int]:
    out = {}
    with pg_conn.cursor() as cur:
        for table in (
            "items", "clusters", "cluster_items", "item_status",
            "cluster_status", "fetch_runs", "cluster_judge_log",
        ):
            cur.execute(f"select count(*) as n from {schema}.{table}")
            out[table] = int(cur.fetchone()["n"])
    return out


def extension_status(pg_conn) -> dict[str, Any]:
    with pg_conn.cursor() as cur:
        cur.execute("select version() as version")
        version = cur.fetchone()["version"]
        cur.execute("select extname from pg_extension where extname = 'vector'")
        vector = cur.fetchone() is not None
    return {"postgres_version": version.split(" on ")[0], "vector_extension": vector}


def local_cosine_top(conn: sqlite3.Connection, item_id: str, cluster_ids: list[int], k: int) -> list[dict[str, Any]]:
    item = conn.execute("select embedding from items where id = ?", (item_id,)).fetchone()
    if item is None or item["embedding"] is None or not cluster_ids:
        return []
    vec = np.frombuffer(item["embedding"], dtype=np.float32)
    rows = conn.execute(
        f"""
        select id, representative_vector
          from clusters
         where id in ({placeholders(cluster_ids)})
           and representative_vector is not null
        """,
        tuple(cluster_ids),
    ).fetchall()
    scored = []
    for row in rows:
        rep = np.frombuffer(row["representative_vector"], dtype=np.float32)
        denom = float(np.linalg.norm(vec) * np.linalg.norm(rep))
        sim = 0.0 if denom == 0.0 else float(np.dot(vec, rep) / denom)
        scored.append({"id": int(row["id"]), "cosine_similarity": sim})
    scored.sort(key=lambda x: x["cosine_similarity"], reverse=True)
    return scored[:k]


def recall_check(pg_conn, sqlite_conn: sqlite3.Connection, schema: str, *, sample_size: int, k: int) -> dict[str, Any]:
    with pg_conn.cursor() as cur:
        cur.execute(f"select id from {schema}.clusters where representative_vector is not null order by id")
        cluster_ids = [int(r["id"]) for r in cur.fetchall()]
    if not cluster_ids:
        return {"checked": 0, "reason": "no remote clusters with vectors"}

    rows = sqlite_conn.execute(
        f"""
        select id, embedding
          from items
         where embedding is not null
           and id in (select item_id from cluster_items where cluster_id in ({placeholders(cluster_ids)}))
         order by fetched_at desc
         limit ?
        """,
        tuple(cluster_ids) + (sample_size,),
    ).fetchall()
    checked = 0
    top1_matches = 0
    overlaps: list[float] = []
    max_abs_errors: list[float] = []
    examples = []
    with pg_conn.cursor() as cur:
        for row in rows:
            remote_sql = f"""
                select id, cosine_similarity
                  from {schema}.match_clusters(%s::{VECTOR_SQL_TYPE}, %s)
            """
            cur.execute(remote_sql, (vector_literal(row["embedding"]), k))
            remote = [{"id": int(r["id"]), "cosine_similarity": float(r["cosine_similarity"])} for r in cur.fetchall()]
            local = local_cosine_top(sqlite_conn, row["id"], cluster_ids, k)
            if not remote or not local:
                continue
            checked += 1
            if remote[0]["id"] == local[0]["id"]:
                top1_matches += 1
            remote_ids = {r["id"] for r in remote}
            local_ids = {r["id"] for r in local}
            overlaps.append(len(remote_ids & local_ids) / max(1, min(k, len(local_ids))))
            local_by_id = {r["id"]: r["cosine_similarity"] for r in local}
            errors = [
                abs(r["cosine_similarity"] - local_by_id[r["id"]])
                for r in remote
                if r["id"] in local_by_id
            ]
            max_abs_errors.append(max(errors) if errors else 0.0)
            if len(examples) < 3:
                examples.append({
                    "item_id": row["id"],
                    "remote_top1": remote[0]["id"],
                    "local_top1": local[0]["id"],
                    "overlap": overlaps[-1],
                })
    return {
        "checked": checked,
        "top1_match_rate": round(top1_matches / checked, 4) if checked else None,
        "avg_topk_overlap": round(sum(overlaps) / checked, 4) if checked else None,
        "max_abs_cosine_error": max(max_abs_errors) if max_abs_errors else None,
        "examples": examples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", default="remote_poc")
    parser.add_argument("--recall-sample", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--skip-recall", action="store_true")
    args = parser.parse_args()

    schema = checked_schema(args.schema)
    load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("SUPABASE_DB_URL is missing. Add it to .env and rerun.")
    pg_conn = pg_connect(db_url)
    sqlite_conn = sqlite_connect(args.db)
    try:
        report: dict[str, Any] = {
            "remote": extension_status(pg_conn),
            "counts": table_counts(pg_conn, schema),
        }
        if not args.skip_recall:
            report["recall"] = recall_check(
                pg_conn,
                sqlite_conn,
                schema,
                sample_size=args.recall_sample,
                k=args.top_k,
            )
    finally:
        sqlite_conn.close()
        pg_conn.close()

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
