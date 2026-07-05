#!/usr/bin/env python3
"""Verify a remote-only Supabase environment without printing secrets."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"env file not found: {path}") from exc

    values: dict[str, str] = {}
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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def apply_env(values: dict[str, str]) -> None:
    for key, value in values.items():
        os.environ[key] = value


def require_count(counts: dict[str, int], table: str, minimum: int) -> None:
    if counts.get(table, 0) < minimum:
        raise SystemExit(f"{table} count below expected minimum: {counts.get(table, 0)} < {minimum}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.staging")
    parser.add_argument("--min-items", type=int, default=1)
    parser.add_argument("--min-clusters", type=int, default=1)
    parser.add_argument("--feed-limit", type=int, default=5)
    parser.add_argument("--skip-write-probe", action="store_true")
    parser.add_argument("--skip-storage-probe", action="store_true")
    parser.add_argument("--skip-api-smoke", action="store_true")
    parser.add_argument(
        "--allow-non-staging",
        action="store_true",
        help="allow probes against an env whose SUPABASE_REMOTE_DB_ENV is not staging",
    )
    args = parser.parse_args()

    env_values = load_env_file(args.env_file)
    if env_values.get("SUPABASE_REMOTE_DB_ENV") != "staging" and not args.allow_non_staging:
        raise SystemExit(
            "env file must set SUPABASE_REMOTE_DB_ENV=staging "
            "(or pass --allow-non-staging)"
        )
    apply_env(env_values)

    import remote_db

    report: dict[str, Any] = {
        "env_file": str(args.env_file),
        "schema": remote_db.remote_schema(),
    }
    report["authority"] = remote_db.assert_remote_authority_ready()
    report["pipeline"] = remote_db.assert_pipeline_write_mode_ready()
    report["storage_contract"] = remote_db.assert_storage_contract_ready()

    feed = remote_db.query_feed(limit=args.feed_limit, min_github_stars=0)
    report["feed"] = {
        "backend": feed.get("data_backend"),
        "returned": len(feed.get("items", [])),
        "total": int(feed.get("total", 0)),
        "first_ids": [item.get("id") for item in feed.get("items", [])[:3]],
    }

    counts: dict[str, int] = {}
    with remote_db.connect() as conn:
        schema = remote_db.remote_schema()
        for table in (
            "items",
            "clusters",
            "cluster_items",
            "item_status",
            "cluster_status",
            "fetch_runs",
            "cluster_judge_log",
            "sync_runs",
            "remote_assets",
        ):
            row = conn.execute(f"select count(*) as n from {schema}.{table}").fetchone()
            counts[table] = int(row["n"] if row else 0)
        counts["item_embeddings"] = int(
            conn.execute(f"select count(*) as n from {schema}.items where embedding is not null").fetchone()["n"]
        )
        counts["cluster_vectors"] = int(
            conn.execute(
                f"select count(*) as n from {schema}.clusters where representative_vector is not null"
            ).fetchone()["n"]
        )
        vector_row = conn.execute(
            f"select embedding::text as emb from {schema}.items where embedding is not null limit 1"
        ).fetchone()
        if vector_row:
            matches = conn.execute(
                f"select id, cosine_similarity from {schema}.match_clusters(%s::extensions.vector, 3)",
                (vector_row["emb"],),
            ).fetchall()
            report["match_clusters"] = {
                "returned": len(matches),
                "top_ids": [int(row["id"]) for row in matches],
            }

        if not args.skip_write_probe:
            smoke_key = "codex_verify_" + uuid.uuid4().hex
            conn.execute(
                f"insert into {schema}.settings(key, value, updated_at) values (%s, %s, now())",
                (smoke_key, "ok"),
            )
            visible = conn.execute(
                f"select value from {schema}.settings where key = %s",
                (smoke_key,),
            ).fetchone()
            report["transactional_write_probe"] = {
                "insert_visible_before_rollback": bool(visible and visible["value"] == "ok"),
                "rolled_back": True,
            }
            conn.rollback()

    require_count(counts, "items", args.min_items)
    require_count(counts, "clusters", args.min_clusters)
    report["counts"] = counts

    if not args.skip_storage_probe:
        object_path = "codex-verify/" + uuid.uuid4().hex + ".jpg"
        blob = None
        cleanup_error = None
        try:
            remote_db.upload_asset_bytes_remote(
                object_path,
                b"\xff\xd8\xff\xd9",
                content_type="image/jpeg",
                kind="verify_remote_only",
            )
            blob = remote_db.download_asset_bytes_remote(object_path)
        finally:
            try:
                remote_db.delete_asset_remote(object_path)
            except Exception as exc:  # pragma: no cover - only when remote cleanup fails
                cleanup_error = f"{type(exc).__name__}: {exc}"
        report["storage_probe"] = {
            "uploaded_downloaded_deleted": blob == b"\xff\xd8\xff\xd9" and cleanup_error is None,
            "cleanup_error": cleanup_error,
        }
        if blob != b"\xff\xd8\xff\xd9" or cleanup_error:
            raise SystemExit("storage probe failed")

    if not args.skip_api_smoke:
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:
            raise SystemExit(
                "FastAPI TestClient unavailable. Install test/runtime deps with "
                "`uv run --with-requirements requirements.txt ...`."
            ) from exc

        import app as app_mod

        client = TestClient(app_mod.app)
        health = client.get("/api/health")
        feed_response = client.get("/api/feed?limit=3&min_github_stars=0")
        events_response = client.get("/api/feed/events?limit=2")

        api_report = {
            "health_status": health.status_code,
            "feed_status": feed_response.status_code,
            "events_status": events_response.status_code,
        }
        if health.status_code == 200:
            api_report["health_data_authority"] = health.json().get("data_authority")
        if feed_response.status_code == 200:
            feed_json = feed_response.json()
            api_report["feed_backend"] = feed_json.get("data_backend")
            api_report["feed_returned"] = len(feed_json.get("items", []))
        if events_response.status_code == 200:
            api_report["events_backend"] = events_response.json().get("data_backend")
        report["api_smoke"] = api_report
        if health.status_code != 200 or feed_response.status_code != 200 or events_response.status_code != 200:
            raise SystemExit("API smoke failed")
        if api_report.get("health_data_authority") != "supabase":
            raise SystemExit("API health did not report data_authority=supabase")
        if api_report.get("feed_backend") not in {"supabase", "supabase_poc", "postgres", "postgres_poc"}:
            raise SystemExit("API feed did not use a remote backend")
        if api_report.get("events_backend") not in {"supabase", "supabase_poc", "postgres", "postgres_poc"}:
            raise SystemExit("API events did not use a remote backend")

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
