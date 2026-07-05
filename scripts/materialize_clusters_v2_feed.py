"""Materialize clean clusters_v2 rows into the legacy event-feed tables.

The current frontend reads /api/feed/events from clusters / cluster_items.
Stage Z/P writes the verified event set to clusters_v2 / cluster_items_v2.
This script bridges those two shapes without rebuilding the v2 run.

Usage:
  python scripts/materialize_clusters_v2_feed.py          # dry-run
  python scripts/materialize_clusters_v2_feed.py --apply  # backup + write
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from time_utils import sort_key, to_utc_iso  # noqa: E402

try:
    from utils.url_normalize import normalize_url  # noqa: E402
except Exception:  # pragma: no cover - defensive fallback for ad-hoc envs
    normalize_url = None  # type: ignore[assignment]


DEFAULT_DB = REPO_ROOT / "data" / "feed.db"
DEFAULT_PROMPT_VERSION = "v5b"

_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", re.I)
_GITHUB_RE = re.compile(r"github\.com/([^/\s?#]+)/([^/\s?#]+)", re.I)


@dataclass(frozen=True)
class FeedCluster:
    v2_id: int
    title: str
    summary: str
    doc_count: int
    unique_source_count: int
    platforms_json: str
    cover_url: str | None
    first_doc_at: str
    last_doc_at: str
    last_updated_at: str
    centroid: bytes | None
    created_at: str
    warnings_json: str
    members: list[sqlite3.Row]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_identity(row: sqlite3.Row) -> str:
    canonical = (row["canonical_url"] or "").strip()
    if canonical:
        return canonical

    url = (row["url"] or "").strip()
    item_id = row["id"]
    platform = (row["platform"] or "").strip()
    if url:
        arxiv = _ARXIV_RE.search(url)
        if arxiv:
            return f"arxiv:{arxiv.group(1).lower()}"
        github = _GITHUB_RE.search(url)
        if github:
            owner = github.group(1).strip().lower()
            repo = github.group(2).strip()
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            return f"github:{owner}/{repo.lower()}"
        if normalize_url is not None:
            try:
                normalized = normalize_url(url)
                if normalized.platform in ("twitter", "youtube") and normalized.canonical_url:
                    return normalized.canonical_url
            except Exception:
                pass
        return url
    if platform == "twitter" and str(item_id).isdigit():
        return f"https://x.com/i/status/{item_id}"
    return str(item_id)


def _event_time(row: sqlite3.Row) -> str | None:
    return to_utc_iso(row["published_at"] or row["fetched_at"])


def _fallback_title(member: sqlite3.Row) -> str:
    for key in ("title", "ai_summary", "content"):
        value = (member[key] or "").strip()
        if value:
            return value[:120]
    return str(member["id"])


def _load_member_rows(conn: sqlite3.Connection, cluster_id: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT i.id, i.platform, i.source, i.title, i.content, i.ai_summary,
                  i.url, i.canonical_url, i.cover_url, i.published_at, i.fetched_at,
                  ci.added_at, ci.joined_cosine
             FROM cluster_items_v2 ci
             JOIN items i ON i.id = ci.item_id
            WHERE ci.cluster_id = ?
              AND ci.removed_at IS NULL""",
        (cluster_id,),
    ).fetchall()
    return sorted(
        rows,
        key=lambda r: (sort_key(r["published_at"] or r["fetched_at"]), str(r["id"])),
        reverse=True,
    )


def _build_feed_clusters(conn: sqlite3.Connection) -> tuple[list[FeedCluster], dict[str, int]]:
    rows = conn.execute(
        """SELECT id, centroid, event_summary, event_certainty, member_count,
                  created_at, last_member_added_at, stage_p_run_at
             FROM clusters_v2
            WHERE stage_p_state = 'clean'
            ORDER BY id"""
    ).fetchall()
    clusters: list[FeedCluster] = []
    stats = {
        "clean": len(rows),
        "materialized": 0,
        "zero_member": 0,
        "member_count_mismatch": 0,
        "low_certainty": 0,
    }
    now_iso = _now_iso()

    for row in rows:
        members = _load_member_rows(conn, int(row["id"]))
        if not members:
            stats["zero_member"] += 1
            continue
        if int(row["member_count"] or 0) != len(members):
            stats["member_count_mismatch"] += 1
        if (row["event_certainty"] or "").strip().lower() == "low":
            stats["low_certainty"] += 1

        platforms = sorted({(m["platform"] or "").strip() for m in members if (m["platform"] or "").strip()})
        identities = {_source_identity(m) for m in members}
        event_times = [_event_time(m) for m in members]
        event_times = [t for t in event_times if t]
        if event_times:
            first_doc_at = min(event_times, key=sort_key)
            last_doc_at = max(event_times, key=sort_key)
        else:
            first_doc_at = last_doc_at = now_iso

        title = (row["event_summary"] or "").strip()
        if not title:
            title = _fallback_title(members[0])
        summary = title

        cover_url = next(((m["cover_url"] or "").strip() for m in members if (m["cover_url"] or "").strip()), None)
        warnings: list[str] = []
        certainty = (row["event_certainty"] or "").strip().lower()
        if certainty == "low":
            warnings.append("stage_p_event_certainty=low")

        clusters.append(
            FeedCluster(
                v2_id=int(row["id"]),
                title=title,
                summary=summary,
                doc_count=len(members),
                unique_source_count=len({x for x in identities if x}),
                platforms_json=json.dumps(platforms, ensure_ascii=False),
                cover_url=cover_url,
                first_doc_at=first_doc_at,
                last_doc_at=last_doc_at,
                last_updated_at=to_utc_iso(row["stage_p_run_at"] or row["last_member_added_at"] or row["created_at"]) or now_iso,
                centroid=row["centroid"],
                created_at=to_utc_iso(row["created_at"]) or now_iso,
                warnings_json=json.dumps(warnings, ensure_ascii=False),
                members=members,
            )
        )

    stats["materialized"] = len(clusters)
    return clusters, stats


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def _backup_db(db_path: Path, prompt_version: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_suffix(f".db.bak.materialize-{prompt_version}-{ts}")
    shutil.copy2(db_path, backup)
    for ext in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + ext)
        if side.exists():
            shutil.copy2(side, backup.with_name(backup.name + ext))
    return backup


def _delete_existing_prompt_version(conn: sqlite3.Connection, prompt_version: str) -> int:
    old_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM clusters WHERE prompt_version = ?",
            (prompt_version,),
        ).fetchall()
    ]
    if not old_ids:
        return 0
    placeholders = ",".join("?" for _ in old_ids)
    conn.execute(f"DELETE FROM cluster_status WHERE cluster_id IN ({placeholders})", old_ids)
    conn.execute(f"DELETE FROM cluster_items WHERE cluster_id IN ({placeholders})", old_ids)
    conn.execute(f"UPDATE items SET cluster_id = NULL WHERE cluster_id IN ({placeholders})", old_ids)
    conn.execute(f"DELETE FROM clusters WHERE id IN ({placeholders})", old_ids)
    return len(old_ids)


def _insert_feed_clusters(
    conn: sqlite3.Connection,
    clusters: list[FeedCluster],
    *,
    prompt_version: str,
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for cluster in clusters:
        cur = conn.execute(
            """INSERT INTO clusters
                 (ai_title, ai_summary, ai_key_points, live_version, doc_count,
                  platforms_json, cover_url, first_doc_at, last_doc_at,
                  last_updated_at, is_visible_in_feed, archived, prompt_version,
                  representative_vector, unique_source_count,
                  last_summary_warnings_json, event_embedding, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cluster.title,
                cluster.summary,
                "[]",
                1,
                cluster.doc_count,
                cluster.platforms_json,
                cluster.cover_url,
                cluster.first_doc_at,
                cluster.last_doc_at,
                cluster.last_updated_at,
                1,
                0,
                prompt_version,
                cluster.centroid,
                cluster.unique_source_count,
                cluster.warnings_json,
                None,
                cluster.created_at,
            ),
        )
        cluster_id = int(cur.lastrowid)
        mapping[cluster.v2_id] = cluster_id
        for rank, member in enumerate(cluster.members):
            conn.execute(
                """INSERT INTO cluster_items
                     (cluster_id, item_id, rank_in_cluster, added_at,
                      is_primary_source, source_identity, join_decision_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    cluster_id,
                    member["id"],
                    rank,
                    cluster.last_updated_at,
                    1 if rank == 0 else 0,
                    _source_identity(member),
                    f"clusters_v2:{cluster.v2_id}",
                ),
            )
            conn.execute(
                "UPDATE items SET cluster_id = ? WHERE id = ?",
                (cluster_id, member["id"]),
            )
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = args.db.resolve()
    if not db_path.exists():
        raise SystemExit(f"DB 不存在：{db_path}")

    conn = _connect(db_path)
    try:
        clusters, stats = _build_feed_clusters(conn)
        old_prompt_rows = _count(
            conn,
            "SELECT COUNT(*) FROM clusters WHERE prompt_version = ?",
            (args.prompt_version,),
        )
        old_prompt_visible = _count(
            conn,
            "SELECT COUNT(*) FROM clusters WHERE prompt_version = ? AND is_visible_in_feed = 1",
            (args.prompt_version,),
        )

        print(f"[materialize] DB: {db_path}")
        print(f"[materialize] prompt_version: {args.prompt_version}")
        print(f"[materialize] clean clusters_v2: {stats['clean']}")
        print(f"[materialize] materializable (visible members > 0): {stats['materialized']}")
        print(f"[materialize] zero-member skipped: {stats['zero_member']}")
        print(f"[materialize] member_count mismatches: {stats['member_count_mismatch']}")
        print(f"[materialize] low-certainty materialized: {stats['low_certainty']}")
        print(f"[materialize] existing {args.prompt_version} rows: {old_prompt_rows} (visible {old_prompt_visible})")

        if not args.apply:
            print("[dry-run] 用 --apply 写入 clusters / cluster_items")
            return 0

        backup = _backup_db(db_path, args.prompt_version)
        print(f"[apply] 备份: {backup}")
        try:
            conn.execute("BEGIN")
            deleted = _delete_existing_prompt_version(conn, args.prompt_version)
            mapping = _insert_feed_clusters(conn, clusters, prompt_version=args.prompt_version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        new_prompt_rows = _count(
            conn,
            "SELECT COUNT(*) FROM clusters WHERE prompt_version = ?",
            (args.prompt_version,),
        )
        new_prompt_visible = _count(
            conn,
            "SELECT COUNT(*) FROM clusters WHERE prompt_version = ? AND is_visible_in_feed = 1",
            (args.prompt_version,),
        )
        new_items = _count(
            conn,
            """SELECT COUNT(*)
                 FROM cluster_items ci
                 JOIN clusters c ON c.id = ci.cluster_id
                WHERE c.prompt_version = ?""",
            (args.prompt_version,),
        )

        print(f"[apply] deleted old {args.prompt_version} rows: {deleted}")
        print(f"[apply] inserted feed clusters: {len(mapping)}")
        print(f"[apply] {args.prompt_version} rows now: {new_prompt_rows} (visible {new_prompt_visible})")
        print(f"[apply] {args.prompt_version} cluster_items now: {new_items}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
