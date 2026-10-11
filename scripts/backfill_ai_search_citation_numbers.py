#!/usr/bin/env python3
"""Renumber completed AI-search citations; dry-run unless --apply is supplied.

Usage: python3 scripts/backfill_ai_search_citation_numbers.py
       python3 scripts/backfill_ai_search_citation_numbers.py --apply --backup-file backup.json

Before any UPDATE, --apply exclusively creates a mode-0600 JSON backup and fsyncs
it. Keep this file even if a later write fails: all database writes are atomic.

Rollback: load the backup JSON's ``rows`` using json.load, connect with this
module's ``connection_kwargs(apply=True)``, and BEGIN one transaction. For each
row execute this parameterized SQL (JSON-encode candidate_ids and cited_ids):

    UPDATE remote_poc.ai_search_jobs
       SET content_md_final = %(old_content_md_final)s
     WHERE id = %(id)s AND status = %(status)s
       AND content_md_final = %(new_content_md_final)s
       AND candidate_ids = %(candidate_ids)s::jsonb
       AND cited_ids = %(cited_ids)s::jsonb
     RETURNING id

Use each saved row as the parameter dictionary. Require exactly one returned ID
per row; otherwise ROLLBACK the entire rollback and investigate the conflicting
ID. COMMIT only after every row matches. The new-content condition prevents
overwriting a later edit. No other columns need restoring. Never use bare SET;
the connection helper selects port 5432 and passes timeouts through options.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_search_digest import renumber_citations  # noqa: E402
from env_utils import load_project_env  # noqa: E402


def _dsn_candidates() -> list[str]:
    """Direct first, pooler second. Some workstations cannot reach the direct host
    (a local proxy swallows it and psycopg reports 198.18.x.x), so the caller falls
    back rather than stopping at a connection error that has nothing to do with the data."""
    project_env = load_project_env(ROOT)
    out = []
    for key in ("SUPABASE_DB_DIRECT_URL", "SUPABASE_DB_URL"):
        value = os.environ.get(key) or project_env.get(key)
        if value and value not in out:
            out.append(value)
    if not out:
        raise ValueError("Set SUPABASE_DB_DIRECT_URL or SUPABASE_DB_URL in env/.env")
    return out


def connection_kwargs(*, apply: bool, dsn: str | None = None) -> dict:
    """Resolve the existing env sources without using remote_db's SET fallback."""
    dsn = dsn or _dsn_candidates()[0]
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("The database setting must be a PostgreSQL URL")
    if any(key.lower() == "port" for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("Query-string port overrides are forbidden; use the URL authority port")
    port = parsed.port or 5432
    if apply and port not in {5432, 6543}:
        raise ValueError("--apply requires the 5432 session port (6543 URLs can be converted)")
    params = conninfo_to_dict(dsn)
    params.update(
        port="5432" if apply else str(port),
        autocommit=True,
        prepare_threshold=None,
        row_factory=dict_row,
        connect_timeout=10,
        application_name="backfill_ai_search_citation_numbers",
        options="-c statement_timeout=30000 -c lock_timeout=5000 "
                "-c idle_in_transaction_session_timeout=60000"
                + (" -c default_transaction_read_only=off" if apply else ""),
    )
    return params


def plan_changes(rows: list[dict]) -> list[dict]:
    changes = []
    for row in rows:
        content = row.get("content_md_final")
        if row.get("status") != "done" or not isinstance(content, str) or not content.strip():
            continue
        job_id = str(row["id"])
        candidates = row.get("candidate_ids")
        if not isinstance(candidates, list) or any(not isinstance(value, str) or not value for value in candidates):
            raise ValueError(f"{job_id}: candidate_ids is not a valid ID list")
        updated, original_numbers = renumber_citations(content, len(candidates))
        if updated == content:
            continue  # Already 1..N: cited_ids refers to that order, not candidate order.
        cited = row.get("cited_ids")
        if [candidates[number - 1] for number in original_numbers] != cited:
            raise ValueError(f"{job_id}: original citation order does not match cited_ids; refusing to rewrite")
        changes.append({
            "id": job_id,
            "status": "done",
            "old_content_md_final": content,
            "new_content_md_final": updated,
            "candidate_ids": candidates,
            "cited_ids": cited,
        })
    return changes


def _write_backup(changes: list[dict], path: Path) -> None:
    data = json.dumps({
        "table": "remote_poc.ai_search_jobs",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rows": changes,
    }, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as backup:
        backup.write(data)
        backup.flush()
        os.fsync(backup.fileno())


def run_backfill(*, apply: bool = False, backup_path: Path | None = None) -> int:
    last = None
    for candidate in _dsn_candidates():
        try:
            conn = psycopg.connect(**connection_kwargs(apply=apply, dsn=candidate))
            break
        except psycopg.OperationalError as exc:      # 连不上就换下一个来源，不是数据问题
            last = exc
            print(f"[conn] {urlsplit(candidate).hostname} 连接失败，换下一个：{str(exc)[:80]}")
    else:
        raise SystemExit(f"ERROR: connection failed: {last}")
    try:
        # autocommit avoids an implicit BEGIN before our explicit read-only BEGIN.
        conn.execute("BEGIN" if apply else "BEGIN READ ONLY")
        rows = conn.execute(
            """SELECT id, status, content_md_final, candidate_ids, cited_ids
                 FROM remote_poc.ai_search_jobs
                WHERE status = %s AND content_md_final IS NOT NULL
                  AND content_md_final <> %s
                ORDER BY id""",
            ("done", ""),
        ).fetchall()
        changes = plan_changes(rows)
        for change in changes:
            print(f"\nJob {change['id']}")
            print("\n".join(difflib.unified_diff(
                change["old_content_md_final"].splitlines(),
                change["new_content_md_final"].splitlines(),
                fromfile="before", tofile="after", lineterm="",
            )))
        if not apply or not changes:
            conn.rollback()
            print(f"{'dry-run' if not apply else 'apply'}: {len(changes)} row(s) to change; no writes")
            return len(changes)
        if backup_path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = Path.cwd() / f"ai-search-citations-{stamp}.json"
        _write_backup(changes, backup_path)
        print(f"Backup saved: {backup_path}")
        for change in changes:
            params = {
                **change,
                "candidate_ids": json.dumps(change["candidate_ids"]),
                "cited_ids": json.dumps(change["cited_ids"]),
            }
            updated = conn.execute(
                """UPDATE remote_poc.ai_search_jobs
                      SET content_md_final = %(new_content_md_final)s
                    WHERE id = %(id)s AND status = %(status)s
                      AND content_md_final = %(old_content_md_final)s
                      AND candidate_ids = %(candidate_ids)s::jsonb
                      AND cited_ids = %(cited_ids)s::jsonb
                    RETURNING id""",
                params,
            ).fetchone()
            if updated is None:
                raise RuntimeError(f"{change['id']}: row changed since planning; rolling back all updates")
        conn.commit()
        print(f"apply: updated {len(changes)} row(s)")
        return len(changes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes after saving a backup; default is dry-run")
    parser.add_argument("--backup-file", type=Path, help="New backup file; default is a timestamped JSON in the current directory")
    args = parser.parse_args()
    try:
        run_backfill(apply=args.apply, backup_path=args.backup_file)
    except (ValueError, OSError, RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
