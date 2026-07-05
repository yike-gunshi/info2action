#!/usr/bin/env python3
"""Sync DB-referenced local image assets to Supabase Storage.

The remote-only app can serve `/images/...` from Supabase Storage, but older
rows may still point at localized files that only exist under a local
`data/images` directory. This script migrates only assets that are referenced by
the remote `items.cover_url` or `items.media_json` fields.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from env_utils import load_project_env  # noqa: E402
import remote_db  # noqa: E402


IMAGE_PREFIX = "/images/"
OBJECT_PREFIX = "images/"
DEFAULT_BATCH_SIZE = 1000
DEFAULT_PROGRESS_EVERY = 100

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


@dataclass
class AssetRef:
    object_path: str
    local_rel_path: str
    ref_count: int = 0
    item_ids: set[str] = field(default_factory=set)
    platforms: Counter[str] = field(default_factory=Counter)
    fields: Counter[str] = field(default_factory=Counter)

    def add(self, *, item_id: str, platform: str | None, field_name: str) -> None:
        self.ref_count += 1
        self.item_ids.add(item_id)
        self.fields[field_name] += 1
        if platform:
            self.platforms[platform] += 1

    @property
    def primary_item_id(self) -> str | None:
        return sorted(self.item_ids)[0] if self.item_ids else None

    @property
    def kind(self) -> str:
        rel = self.local_rel_path
        if rel.startswith("images/video_posters/"):
            return "video_poster"
        if rel.endswith(".html"):
            return "html"
        return "image"


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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    os.environ.update(values)
    return values


def normalize_image_reference(value: str) -> tuple[str, str] | None:
    """Return `(object_path, local_rel_path)` for a safe `/images/...` ref."""
    if not isinstance(value, str) or not value.startswith(IMAGE_PREFIX):
        return None
    clean = value.split("?", 1)[0].split("#", 1)[0]
    rel = clean[len(IMAGE_PREFIX) :].lstrip("/")
    if not rel or "\\" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    normalized = "/".join(parts)
    object_path = OBJECT_PREFIX + normalized
    return object_path, object_path


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for entry in value:
            yield from _walk_strings(entry)
    elif isinstance(value, dict):
        for entry in value.values():
            yield from _walk_strings(entry)


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def collect_referenced_assets(rows: Iterable[dict[str, Any]]) -> dict[str, AssetRef]:
    assets: dict[str, AssetRef] = {}
    for row in rows:
        item_id = str(row.get("id") or "")
        platform = row.get("platform") or None
        cover = row.get("cover_url") or ""
        normalized_cover = normalize_image_reference(cover)
        if normalized_cover:
            object_path, local_rel_path = normalized_cover
            assets.setdefault(
                object_path, AssetRef(object_path=object_path, local_rel_path=local_rel_path)
            ).add(item_id=item_id, platform=platform, field_name="cover_url")

        media = _json_value(row.get("media_json"))
        for value in _walk_strings(media):
            normalized_media = normalize_image_reference(value)
            if not normalized_media:
                continue
            object_path, local_rel_path = normalized_media
            assets.setdefault(
                object_path, AssetRef(object_path=object_path, local_rel_path=local_rel_path)
            ).add(item_id=item_id, platform=platform, field_name="media_json")
    return assets


def content_type_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def iter_remote_asset_rows(batch_size: int = DEFAULT_BATCH_SIZE) -> Iterable[dict[str, Any]]:
    sql = f"""
        SELECT id, platform, cover_url, media_json
        FROM {remote_db.remote_schema()}.items
        WHERE cover_url LIKE '/images/%'
           OR media_json::text LIKE '%/images/%'
        ORDER BY id
    """
    with remote_db.connect() as conn:
        cur = conn.execute(sql)
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield from rows


def existing_remote_asset_paths(object_paths: list[str], batch_size: int = DEFAULT_BATCH_SIZE) -> set[str]:
    existing: set[str] = set()
    if not object_paths:
        return existing
    with remote_db.connect() as conn:
        for start in range(0, len(object_paths), batch_size):
            batch = object_paths[start : start + batch_size]
            cur = conn.execute(
                f"""
                SELECT object_path
                FROM {remote_db.remote_schema()}.remote_assets
                WHERE object_path = ANY(%s)
                """,
                (batch,),
            )
            existing.update(str(row["object_path"]) for row in cur.fetchall())
    return existing


def summarize_assets(
    assets: dict[str, AssetRef],
    *,
    local_data_dir: Path,
    existing_paths: set[str],
    selected_paths: set[str] | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    selected_paths = selected_paths or set(assets)
    by_ext: Counter[str] = Counter()
    by_platform: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    missing: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    total_present_bytes = 0
    total_upload_bytes = 0
    present_count = 0
    upload_count = 0
    existing_count = 0

    for object_path in sorted(selected_paths):
        asset = assets[object_path]
        local_path = local_data_dir / asset.local_rel_path
        exists_local = local_path.is_file()
        size = local_path.stat().st_size if exists_local else 0
        is_existing = object_path in existing_paths
        ext = local_path.suffix.lower() or "<none>"
        by_ext[ext] += 1
        by_platform.update(asset.platforms)
        by_kind[asset.kind] += 1
        if exists_local:
            present_count += 1
            total_present_bytes += size
        else:
            missing.append(
                {
                    "object_path": object_path,
                    "referenced_by": sorted(asset.item_ids)[:5],
                    "platforms": dict(asset.platforms),
                }
            )
        if is_existing:
            existing_count += 1
        elif exists_local:
            upload_count += 1
            total_upload_bytes += size
        if len(selected) < sample_limit:
            selected.append(
                {
                    "object_path": object_path,
                    "exists_local": exists_local,
                    "exists_remote_metadata": is_existing,
                    "size_bytes": size,
                    "content_type": content_type_for(object_path),
                    "ref_count": asset.ref_count,
                    "platforms": dict(asset.platforms),
                }
            )

    return {
        "selected_assets": len(selected_paths),
        "selected_references": sum(assets[path].ref_count for path in selected_paths),
        "local_present": present_count,
        "local_missing": len(missing),
        "remote_metadata_existing": existing_count,
        "upload_candidates": upload_count,
        "present_bytes": total_present_bytes,
        "upload_bytes": total_upload_bytes,
        "present_mib": round(total_present_bytes / 1024 / 1024, 3),
        "upload_mib": round(total_upload_bytes / 1024 / 1024, 3),
        "by_extension": dict(by_ext),
        "by_platform": dict(by_platform),
        "by_kind": dict(by_kind),
        "missing_assets": missing,
        "missing_samples": missing[:sample_limit],
        "asset_samples": selected,
    }


def build_manifest(
    assets: dict[str, AssetRef],
    *,
    local_data_dir: Path,
    existing_paths: set[str],
    selected_paths: set[str],
    dry_run: bool,
    confirm_upload: bool,
    force: bool,
    upload_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_paths = set(assets)
    return {
        "dry_run": dry_run,
        "confirm_upload": confirm_upload,
        "force": force,
        "env": os.environ.get("SUPABASE_REMOTE_DB_ENV") or "unknown",
        "schema": remote_db.remote_schema(),
        "bucket": remote_db.supabase_storage_bucket(),
        "local_data_dir": str(local_data_dir),
        "scope": summarize_assets(
            assets,
            local_data_dir=local_data_dir,
            existing_paths=existing_paths,
            selected_paths=all_paths,
        ),
        "operation": summarize_assets(
            assets,
            local_data_dir=local_data_dir,
            existing_paths=existing_paths,
            selected_paths=selected_paths,
        ),
        "upload_result": upload_result or {},
    }


def compact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a stdout-friendly view while keeping the file manifest complete."""

    def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "selected_assets",
            "selected_references",
            "local_present",
            "local_missing",
            "remote_metadata_existing",
            "upload_candidates",
            "present_bytes",
            "upload_bytes",
            "present_mib",
            "upload_mib",
            "by_extension",
            "by_platform",
            "by_kind",
        ]
        compact = {key: summary.get(key) for key in keys}
        compact["missing_samples"] = summary.get("missing_samples", [])[:5]
        compact["asset_samples"] = summary.get("asset_samples", [])[:5]
        return compact

    upload_result = manifest.get("upload_result") or {}
    compact_upload = {
        key: upload_result.get(key)
        for key in [
            "uploaded",
            "skipped_existing",
            "skipped_missing",
            "failed",
            "uploaded_bytes",
            "uploaded_mib",
        ]
        if key in upload_result
    }
    if "failures" in upload_result:
        compact_upload["failure_samples"] = upload_result.get("failures", [])[:5]
    if "missing" in upload_result:
        compact_upload["missing_samples"] = upload_result.get("missing", [])[:5]

    return {
        "dry_run": manifest["dry_run"],
        "confirm_upload": manifest["confirm_upload"],
        "force": manifest["force"],
        "env": manifest["env"],
        "schema": manifest["schema"],
        "bucket": manifest["bucket"],
        "local_data_dir": manifest["local_data_dir"],
        "scope": compact_summary(manifest["scope"]),
        "operation": compact_summary(manifest["operation"]),
        "upload_result": compact_upload,
    }


def upload_assets(
    assets: dict[str, AssetRef],
    *,
    local_data_dir: Path,
    selected_paths: list[str],
    existing_paths: set[str],
    force: bool,
    workers: int,
    retries: int,
    metadata_batch_size: int,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "uploaded": 0,
        "skipped_existing": 0,
        "skipped_missing": 0,
        "failed": 0,
        "uploaded_bytes": 0,
        "failures": [],
        "missing": [],
    }
    def flush_metadata(rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        sql = f"""INSERT INTO {remote_db.remote_schema()}.remote_assets
                    (object_path, bucket, content_type, size_bytes, source_item_id, kind, updated_at)
                  VALUES (%s, %s, %s, %s, %s, %s, now())
                  ON CONFLICT (object_path) DO UPDATE SET
                    bucket = excluded.bucket,
                    content_type = excluded.content_type,
                    size_bytes = excluded.size_bytes,
                    source_item_id = COALESCE(excluded.source_item_id, {remote_db.remote_schema()}.remote_assets.source_item_id),
                    kind = COALESCE(excluded.kind, {remote_db.remote_schema()}.remote_assets.kind),
                    updated_at = excluded.updated_at"""
        values = [
            (
                row["object_path"],
                remote_db.supabase_storage_bucket(),
                row["content_type"],
                row["bytes"],
                row["source_item_id"],
                row["kind"],
            )
            for row in rows
        ]
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with remote_db.connect() as conn:
                    with conn.cursor() as cur:
                        cur.executemany(sql, values)
                    conn.commit()
                return
            except Exception as exc:  # noqa: BLE001 - retry remote metadata write
                last_error = exc
                if attempt < retries:
                    time.sleep(min(2**attempt, 8))
                    continue
        raise last_error or RuntimeError("metadata flush failed")

    def upload_one(object_path: str) -> dict[str, Any]:
        asset = assets[object_path]
        local_path = local_data_dir / asset.local_rel_path
        if object_path in existing_paths and not force:
            return {"status": "existing", "object_path": object_path}
        if not local_path.is_file():
            return {
                "status": "missing",
                "object_path": object_path,
                "referenced_by": sorted(asset.item_ids)[:5],
            }
        last_error = ""
        for attempt in range(retries + 1):
            try:
                data = local_path.read_bytes()
                content_type = content_type_for(object_path)
                req = urllib.request.Request(
                    remote_db._storage_object_url(object_path),
                    data=data,
                    method="POST",
                    headers=remote_db._storage_headers(content_type, upsert=True),
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
                return {
                    "status": "uploaded",
                    "object_path": object_path,
                    "bytes": len(data),
                    "content_type": content_type,
                    "source_item_id": asset.primary_item_id,
                    "kind": asset.kind,
                }
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:300]
                last_error = f"HTTP {exc.code} {body}"
                if attempt < retries:
                    time.sleep(min(2**attempt, 8))
                    continue
            except Exception as exc:  # noqa: BLE001 - report and continue the batch
                last_error = str(exc)[:300]
                if attempt < retries:
                    time.sleep(min(2**attempt, 8))
                    continue
        return {"status": "failed", "object_path": object_path, "error": last_error}

    total = len(selected_paths)
    completed = 0
    max_workers = max(1, workers)
    metadata_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(upload_one, object_path) for object_path in selected_paths]
        for future in as_completed(futures):
            item = future.result()
            completed += 1
            status = item["status"]
            if status == "uploaded":
                result["uploaded"] += 1
                result["uploaded_bytes"] += item["bytes"]
                metadata_rows.append(item)
                if len(metadata_rows) >= metadata_batch_size:
                    flush_metadata(metadata_rows)
                    metadata_rows.clear()
            elif status == "existing":
                result["skipped_existing"] += 1
            elif status == "missing":
                result["skipped_missing"] += 1
                result["missing"].append(
                    {"object_path": item["object_path"], "referenced_by": item["referenced_by"]}
                )
            else:
                result["failed"] += 1
                result["failures"].append(
                    {"object_path": item["object_path"], "error": item["error"]}
                )
            if progress_every > 0 and (completed % progress_every == 0 or completed == total):
                print(
                    f"[asset-sync] {completed}/{total} uploaded={result['uploaded']} "
                    f"existing={result['skipped_existing']} missing={result['skipped_missing']} "
                    f"failed={result['failed']}",
                    file=sys.stderr,
                    flush=True,
                )
    flush_metadata(metadata_rows)
    result["uploaded_mib"] = round(result["uploaded_bytes"] / 1024 / 1024, 3)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--local-data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--prefer-direct-db", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="limit selected assets for smoke uploads")
    parser.add_argument("--confirm-upload", action="store_true", help="actually upload assets")
    parser.add_argument("--force", action="store_true", help="upload even when remote_assets metadata exists")
    parser.add_argument("--fail-on-missing", action="store_true")
    parser.add_argument("--workers", type=int, default=8, help="parallel upload workers")
    parser.add_argument("--retries", type=int, default=2, help="per-object upload retries")
    parser.add_argument("--metadata-batch-size", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env_file(args.env_file)
    # Keep values from the selected env ahead of the project `.env`.
    os.environ.update({k: v for k, v in load_project_env(ROOT).items() if k not in os.environ})
    if args.prefer_direct_db and os.environ.get("SUPABASE_DB_DIRECT_URL"):
        os.environ["SUPABASE_DB_URL"] = os.environ["SUPABASE_DB_DIRECT_URL"]

    local_data_dir = args.local_data_dir.expanduser().resolve()
    rows = list(iter_remote_asset_rows(batch_size=args.batch_size))
    assets = collect_referenced_assets(rows)
    all_paths = sorted(assets)
    selected_paths = all_paths[: args.limit] if args.limit and args.limit > 0 else all_paths
    existing_paths = existing_remote_asset_paths(all_paths, batch_size=args.batch_size)

    dry_run = not args.confirm_upload
    upload_result: dict[str, Any] | None = None
    if args.confirm_upload:
        upload_result = upload_assets(
            assets,
            local_data_dir=local_data_dir,
            selected_paths=selected_paths,
            existing_paths=existing_paths,
            force=args.force,
            workers=args.workers,
            retries=args.retries,
            metadata_batch_size=args.metadata_batch_size,
            progress_every=args.progress_every,
        )

    manifest = build_manifest(
        assets,
        local_data_dir=local_data_dir,
        existing_paths=existing_paths,
        selected_paths=set(selected_paths),
        dry_run=dry_run,
        confirm_upload=args.confirm_upload,
        force=args.force,
        upload_result=upload_result,
    )
    body = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(body + "\n", encoding="utf-8")
        print(json.dumps(compact_manifest(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(body)

    missing = manifest["operation"]["local_missing"]
    failures = (upload_result or {}).get("failed", 0)
    if args.fail_on_missing and missing:
        print(f"local referenced assets missing: {missing}", file=sys.stderr)
        return 2
    if failures:
        print(f"asset upload failures: {failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
