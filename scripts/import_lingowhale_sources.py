#!/usr/bin/env python3
"""Import Lingowhale subscribed WeChat channels into the sources registry.

Reads data/lingowhale/groups.json and upserts one wechat_mp source per unique
channel_id. Existing source status and origin are preserved so admin changes
survive repeated imports.
"""
import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db  # noqa: E402
import remote_db  # noqa: E402

_LINGOWHALE_CONFIG = json.dumps({"backend": "lingowhale"}, ensure_ascii=False)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def iter_lingowhale_channels(groups):
    """Yield unique {channel_id, name} entries from a Lingowhale groups snapshot."""
    seen = set()

    def walk(obj):
        if isinstance(obj, dict):
            channel_id = str(obj.get("channel_id") or "").strip()
            if channel_id and channel_id not in {"all", "topic"} and channel_id not in seen:
                seen.add(channel_id)
                name = str(obj.get("name") or channel_id).strip() or channel_id
                yield {"channel_id": channel_id, "name": name}
            for value in obj.values():
                yield from walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from walk(value)

    yield from walk(groups)


def _import_into(conn, groups, *, remote):
    summary = {"inserted": 0, "updated": 0, "seen": 0}
    for channel in iter_lingowhale_channels(groups):
        summary["seen"] += 1
        channel_id = channel["channel_id"]
        display_name = channel["name"]
        now = _now()
        if remote:
            action = remote_db.upsert_source_registry_remote(
                conn,
                platform="wechat_mp",
                source_key=channel_id,
                display_name=display_name,
                status="active",
                config_json=_LINGOWHALE_CONFIG,
                origin="lingowhale_import",
                now=now,
            )
        else:
            row = conn.execute(
                "SELECT id FROM sources WHERE platform = 'wechat_mp' AND source_key = ?",
                (channel_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE sources
                          SET display_name = ?, config_json = ?, updated_at = ?
                        WHERE id = ?""",
                    (display_name, _LINGOWHALE_CONFIG, now, row["id"]),
                )
                action = "updated"
            else:
                conn.execute(
                    """INSERT INTO sources(platform, source_key, display_name, status,
                                           config_json, origin, created_at, updated_at)
                       VALUES('wechat_mp', ?, ?, 'active', ?, 'lingowhale_import', ?, ?)""",
                    (channel_id, display_name, _LINGOWHALE_CONFIG, now, now),
                )
                action = "inserted"
        summary[action] += 1
    conn.commit()
    return summary


def import_lingowhale_sources(conn=None, groups_path=None, base=BASE):
    """Idempotently import Lingowhale channels. Returns {inserted, updated, seen}."""
    groups_path = groups_path or os.path.join(base, "data", "lingowhale", "groups.json")
    groups = _load_json(groups_path)
    if conn is not None:
        return _import_into(conn, groups, remote=False)
    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as remote_conn:
            return _import_into(remote_conn, groups, remote=True)

    local_conn = db.get_conn()
    try:
        return _import_into(local_conn, groups, remote=False)
    finally:
        local_conn.close()


def main():
    summary = import_lingowhale_sources()
    print("=== Lingowhale sources import complete ===")
    print(
        f"  seen {summary['seen']}  inserted {summary['inserted']}  "
        f"updated {summary['updated']}"
    )


if __name__ == "__main__":
    main()
