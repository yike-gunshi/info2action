#!/usr/bin/env python3
"""Export the sources registry to a daily JSON snapshot."""
import json
import os
import re
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db  # noqa: E402

_SNAPSHOT_RE = re.compile(r"^sources-(\d{8})\.json$")


def _snapshot_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _source_dict(row):
    data = dict(row)
    raw = data.get("config_json")
    if raw:
        try:
            data["config_json"] = json.loads(raw)
        except (TypeError, ValueError):
            pass
    return data


def _cleanup_old_snapshots(backup_dir, keep=30):
    snapshots = []
    for name in os.listdir(backup_dir):
        match = _SNAPSHOT_RE.fullmatch(name)
        if match:
            snapshots.append((match.group(1), name))
    snapshots.sort(reverse=True)

    cleaned = 0
    for _stamp, name in snapshots[keep:]:
        os.remove(os.path.join(backup_dir, name))
        cleaned += 1
    return cleaned


def snapshot_sources(base=BASE):
    backup_dir = os.path.join(base, "data", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
        data = [_source_dict(row) for row in rows]
    finally:
        conn.close()

    path = os.path.join(backup_dir, f"sources-{_snapshot_stamp()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    cleaned = _cleanup_old_snapshots(backup_dir)
    print(f"exported {len(data)} sources to {path}; cleaned {cleaned} old snapshots")
    return {"path": path, "rows": len(data), "cleaned": cleaned}


def main():
    snapshot_sources()


if __name__ == "__main__":
    main()
