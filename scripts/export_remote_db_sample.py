#!/usr/bin/env python3
"""Export a deterministic SQLite sample for the remote DB POC.

The output is intentionally JSONL and safe to inspect locally. Vector BLOBs are
not expanded to full float arrays here; we only record their dimensions and byte
sizes so the sample manifest stays light. The sync script reads vectors directly
from SQLite when writing to Postgres.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "feed.db"
DEFAULT_OUT = ROOT / "data" / "remote-db-poc" / "s0"
VECTOR_DIM = 1536
TABLES = ("items", "clusters", "cluster_items", "fetch_runs", "cluster_judge_log")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def vector_meta(blob: bytes | None) -> dict[str, Any] | None:
    if blob is None:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    return {
        "bytes": len(blob),
        "dim": int(arr.shape[0]),
        "valid_dim": bool(arr.shape[0] == VECTOR_DIM),
        "l2_norm": float(np.linalg.norm(arr)),
    }


def jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"_blob": True, **(vector_meta(bytes(value)) or {})}
    return value


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: jsonish(row[k]) for k in row.keys()}


def write_jsonl(path: Path, rows: Iterable[sqlite3.Row]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row_dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def ordered_unique(values: Iterable[Any]) -> list[Any]:
    seen = set()
    out = []
    for value in values:
        if value is None or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def placeholders(values: list[Any]) -> str:
    if not values:
        raise ValueError("placeholders requires at least one value")
    return ",".join("?" for _ in values)


def sample_ids(conn: sqlite3.Connection, *, items_limit: int, clusters_limit: int) -> tuple[list[str], list[int]]:
    cluster_rows = conn.execute(
        """
        SELECT id
          FROM clusters
         WHERE representative_vector IS NOT NULL
         ORDER BY COALESCE(published_at, last_updated_at, created_at) DESC, id DESC
         LIMIT ?
        """,
        (clusters_limit,),
    ).fetchall()
    cluster_ids = [int(r["id"]) for r in cluster_rows]

    member_item_ids: list[str] = []
    if cluster_ids:
        member_item_ids = [
            str(r["item_id"])
            for r in conn.execute(
                f"""
                SELECT item_id
                  FROM cluster_items
                 WHERE cluster_id IN ({placeholders(cluster_ids)})
                 ORDER BY cluster_id, COALESCE(rank_in_cluster, 9999), item_id
                """,
                tuple(cluster_ids),
            ).fetchall()
        ]

    recent_item_ids = [
        str(r["id"])
        for r in conn.execute(
            """
            SELECT id
              FROM items
             ORDER BY COALESCE(published_at, fetched_at) DESC, fetched_at DESC, id DESC
             LIMIT ?
            """,
            (items_limit,),
        ).fetchall()
    ]
    embedded_item_ids = [
        str(r["id"])
        for r in conn.execute(
            """
            SELECT id
              FROM items
             WHERE embedding IS NOT NULL
             ORDER BY COALESCE(published_at, fetched_at) DESC, fetched_at DESC, id DESC
             LIMIT ?
            """,
            (max(25, items_limit // 4),),
        ).fetchall()
    ]
    item_ids = ordered_unique([*member_item_ids, *recent_item_ids, *embedded_item_ids])[:items_limit]
    return item_ids, cluster_ids


def export_sample(conn: sqlite3.Connection, out_dir: Path, *, items_limit: int, clusters_limit: int) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    item_ids, cluster_ids = sample_ids(conn, items_limit=items_limit, clusters_limit=clusters_limit)
    manifest: dict[str, Any] = {
        "items_limit": items_limit,
        "clusters_limit": clusters_limit,
        "item_ids": len(item_ids),
        "cluster_ids": len(cluster_ids),
        "tables": {},
        "vector_dim": VECTOR_DIM,
    }

    if item_ids:
        rows = conn.execute(
            f"SELECT * FROM items WHERE id IN ({placeholders(item_ids)}) ORDER BY fetched_at DESC, id DESC",
            tuple(item_ids),
        ).fetchall()
    else:
        rows = []
    manifest["tables"]["items"] = write_jsonl(out_dir / "items.jsonl", rows)

    if cluster_ids:
        rows = conn.execute(
            f"SELECT * FROM clusters WHERE id IN ({placeholders(cluster_ids)}) ORDER BY id",
            tuple(cluster_ids),
        ).fetchall()
    else:
        rows = []
    manifest["tables"]["clusters"] = write_jsonl(out_dir / "clusters.jsonl", rows)

    if cluster_ids and item_ids:
        rows = conn.execute(
            f"""
            SELECT *
              FROM cluster_items
             WHERE cluster_id IN ({placeholders(cluster_ids)})
               AND item_id IN ({placeholders(item_ids)})
             ORDER BY cluster_id, COALESCE(rank_in_cluster, 9999), item_id
            """,
            tuple(cluster_ids) + tuple(item_ids),
        ).fetchall()
    else:
        rows = []
    manifest["tables"]["cluster_items"] = write_jsonl(out_dir / "cluster_items.jsonl", rows)

    fetch_run_ids = []
    if item_ids:
        fetch_run_ids = [
            int(r["fetch_run_id"])
            for r in conn.execute(
                f"""
                SELECT DISTINCT fetch_run_id
                  FROM items
                 WHERE id IN ({placeholders(item_ids)})
                   AND fetch_run_id IS NOT NULL
                """,
                tuple(item_ids),
            ).fetchall()
        ]
    if fetch_run_ids:
        rows = conn.execute(
            f"SELECT * FROM fetch_runs WHERE id IN ({placeholders(fetch_run_ids)}) ORDER BY id",
            tuple(fetch_run_ids),
        ).fetchall()
    else:
        rows = []
    manifest["tables"]["fetch_runs"] = write_jsonl(out_dir / "fetch_runs.jsonl", rows)

    if item_ids:
        rows = conn.execute(
            f"""
            SELECT *
              FROM cluster_judge_log
             WHERE item_id IN ({placeholders(item_ids)})
             ORDER BY id
             LIMIT 1000
            """,
            tuple(item_ids),
        ).fetchall()
    else:
        rows = []
    manifest["tables"]["cluster_judge_log"] = write_jsonl(out_dir / "cluster_judge_log.jsonl", rows)

    bad_vectors = {"items": 0, "clusters": 0}
    if item_ids:
        bad_vectors["items"] = conn.execute(
            f"""
            SELECT COUNT(*) AS n
              FROM items
             WHERE id IN ({placeholders(item_ids)})
               AND embedding IS NOT NULL
               AND length(embedding) != ?
            """,
            tuple(item_ids) + (VECTOR_DIM * 4,),
        ).fetchone()["n"]
    if cluster_ids:
        bad_vectors["clusters"] = conn.execute(
            f"""
            SELECT COUNT(*) AS n
              FROM clusters
             WHERE id IN ({placeholders(cluster_ids)})
               AND representative_vector IS NOT NULL
               AND length(representative_vector) != ?
            """,
            tuple(cluster_ids) + (VECTOR_DIM * 4,),
        ).fetchone()["n"]
    manifest["bad_vector_dimensions"] = bad_vectors

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--items-limit", type=int, default=200)
    parser.add_argument("--clusters-limit", type=int, default=50)
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        manifest = export_sample(
            conn,
            args.out_dir,
            items_limit=args.items_limit,
            clusters_limit=args.clusters_limit,
        )
    finally:
        conn.close()
    print(json.dumps({
        "out_dir": str(args.out_dir),
        "tables": manifest["tables"],
        "bad_vector_dimensions": manifest["bad_vector_dimensions"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
