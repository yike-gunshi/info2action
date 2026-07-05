#!/usr/bin/env python3
"""Seed Lingowhale group metadata into the remote settings table.

This keeps /api/lingowhale/groups independent from a worktree-local
data/lingowhale/groups.json file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


def _load_groups(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise SystemExit(f"{path} must contain a JSON array")
    groups = [group for group in data if isinstance(group, dict) and group.get("name")]
    if not groups:
        raise SystemExit(f"{path} has no usable Lingowhale groups")
    return groups


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--groups-path",
        type=Path,
        default=ROOT / "data" / "lingowhale" / "groups.json",
        help="Path to Lingowhale groups.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print summary without writing remote settings",
    )
    args = parser.parse_args()

    groups_path = args.groups_path.expanduser()
    if not groups_path.exists():
        raise SystemExit(f"groups file not found: {groups_path}")

    groups = _load_groups(groups_path)
    channel_count = sum(len(group.get("channels") or []) for group in groups)
    print(f"lingowhale_groups groups={len(groups)} channels={channel_count} source={groups_path}")

    if args.dry_run:
        print("dry_run=1 write_skipped=1")
        return

    remote_db.set_lingowhale_groups_metadata_remote(groups)
    print(f"remote_settings_key={remote_db.LINGOWHALE_GROUPS_SETTING_KEY} write=ok")


if __name__ == "__main__":
    main()
