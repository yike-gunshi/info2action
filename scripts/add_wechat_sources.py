#!/usr/bin/env python3
"""Resolve Lingowhale public accounts and add exact matches to sources."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sys


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import db  # noqa: E402
import remote_db  # noqa: E402
from fetch_lingowhale import search_channels  # noqa: E402


PLATFORM = "wechat_mp"
CONFIG_JSON = json.dumps({"backend": "lingowhale"}, ensure_ascii=False)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value):
    return " ".join(str(value or "").split())


def _truncate(value, limit=60):
    text = _one_line(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def resolve_name(query, search_fn=search_channels):
    candidates = search_fn(query, limit=20)
    official_matches = [
        item for item in candidates if item.get("name") == f"{query}-公众号"
    ]
    if len(official_matches) == 1:
        match = official_matches[0]
    elif official_matches:
        match = None
    else:
        match = next((item for item in candidates if item.get("name") == query), None)
    return {"query": query, "candidates": candidates, "match": match}


def _print_candidate(candidate, index, indent="  "):
    print(
        f"{indent}{index}. name={_one_line(candidate.get('name'))} | "
        f"channel_id={_one_line(candidate.get('channel_id'))} | "
        f"description={_truncate(candidate.get('description'))} | "
        f"subscribe_user_count={candidate.get('subscriber_count', 0)} | "
        f"last_7_article_count={candidate.get('last_7d_count', 0)}"
    )


def print_resolution(result):
    query = result["query"]
    candidates = result["candidates"]
    match = result["match"]
    print(f"\nQUERY {query}")
    if candidates:
        print(f"  candidates ({len(candidates)}):")
        for index, candidate in enumerate(candidates, 1):
            _print_candidate(candidate, index, indent="    ")
    else:
        print("  candidates (0)")

    if match:
        print(
            f"  EXACT name={_one_line(match.get('name'))} "
            f"channel_id={_one_line(match.get('channel_id'))}"
        )
        return

    print(f"  AMBIGUOUS {query}")
    if candidates:
        print("  top 5 candidates:")
        for index, candidate in enumerate(candidates[:5], 1):
            _print_candidate(candidate, index, indent="    ")


@contextmanager
def registry_connection():
    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as conn:
            yield conn, True
        return

    conn = db.get_conn()
    try:
        yield conn, False
    finally:
        conn.close()


def insert_source(conn, *, remote, channel_id, name):
    display_name = f"{name}-公众号"
    if remote:
        schema = remote_db.remote_schema()
        existing = conn.execute(
            f"SELECT source_key FROM {schema}.sources "
            "WHERE platform=%s AND source_key=%s LIMIT 1",
            (PLATFORM, channel_id),
        ).fetchone()
        if existing:
            return {"action": "skipped_exists", "name_conflicts": []}
        conflicts = conn.execute(
            f"SELECT source_key FROM {schema}.sources "
            "WHERE platform=%s AND display_name=%s AND source_key<>%s LIMIT 5",
            (PLATFORM, display_name, channel_id),
        ).fetchall()
        now = _now()
        conn.execute(
            f"INSERT INTO {schema}.sources"
            "(platform, source_key, display_name, status, config_json, origin, "
            "created_at, updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                PLATFORM,
                channel_id,
                display_name,
                "active",
                CONFIG_JSON,
                "admin_add",
                now,
                now,
            ),
        )
    else:
        existing = conn.execute(
            "SELECT source_key FROM sources "
            "WHERE platform=? AND source_key=? LIMIT 1",
            (PLATFORM, channel_id),
        ).fetchone()
        if existing:
            return {"action": "skipped_exists", "name_conflicts": []}
        conflicts = conn.execute(
            "SELECT source_key FROM sources "
            "WHERE platform=? AND display_name=? AND source_key<>? LIMIT 5",
            (PLATFORM, display_name, channel_id),
        ).fetchall()
        now = _now()
        conn.execute(
            "INSERT INTO sources(platform, source_key, display_name, status, "
            "config_json, origin, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                PLATFORM,
                channel_id,
                display_name,
                "active",
                CONFIG_JSON,
                "admin_add",
                now,
                now,
            ),
        )
    conn.commit()
    return {
        "action": "inserted",
        "name_conflicts": [row["source_key"] for row in conflicts],
    }


def _empty_summary():
    return {
        "resolved": 0,
        "inserted": 0,
        "skipped_exists": 0,
        "ambiguous": 0,
        "failed": 0,
        "ambiguous_names": [],
        "failed_names": [],
    }


def _resolve_inputs(names, summary):
    resolved = []
    for query in names:
        try:
            result = resolve_name(query)
        except Exception as exc:
            print(f"FAILED {query}: {exc}", file=sys.stderr)
            summary["failed"] += 1
            summary["failed_names"].append(f"{query}: {exc}")
            continue
        print_resolution(result)
        if result["match"]:
            summary["resolved"] += 1
            resolved.append((query, result["match"]))
        else:
            summary["ambiguous"] += 1
            summary["ambiguous_names"].append(query)
    return resolved


def _apply_resolved(records, summary):
    if not records:
        return
    try:
        with registry_connection() as (conn, remote):
            for name, candidate in records:
                channel_id = str(candidate.get("channel_id") or "").strip()
                if not channel_id:
                    message = f"{name}: exact match is missing channel_id"
                    print(f"FAILED {message}", file=sys.stderr)
                    summary["failed"] += 1
                    summary["failed_names"].append(message)
                    continue
                try:
                    result = insert_source(
                        conn,
                        remote=remote,
                        channel_id=channel_id,
                        name=name,
                    )
                except Exception as exc:
                    conn.rollback()
                    print(f"FAILED {name}: registry write failed: {exc}", file=sys.stderr)
                    summary["failed"] += 1
                    summary["failed_names"].append(f"{name}: {exc}")
                    continue

                for existing_key in result["name_conflicts"]:
                    print(
                        f"WARN-NAME-EXISTS display_name={name}-公众号 "
                        f"existing_channel_id={existing_key} new_channel_id={channel_id}"
                    )
                if result["action"] == "skipped_exists":
                    print(f"SKIP-EXISTS {name} channel_id={channel_id}")
                    summary["skipped_exists"] += 1
                else:
                    print(f"INSERTED {name} channel_id={channel_id}")
                    summary["inserted"] += 1
    except Exception as exc:
        print(f"FAILED registry connection: {exc}", file=sys.stderr)
        summary["failed"] += len(records)
        summary["failed_names"].extend(
            f"{name}: registry connection failed: {exc}" for name, _ in records
        )


def print_summary(summary):
    print("\n=== SUMMARY ===")
    for label, key in (
        ("resolved", "resolved"),
        ("inserted", "inserted"),
        ("skipped-exists", "skipped_exists"),
        ("ambiguous", "ambiguous"),
        ("failed", "failed"),
    ):
        print(f"  {label:16} {summary[key]}")
    if summary["ambiguous_names"]:
        print("  ambiguous names:")
        for name in summary["ambiguous_names"]:
            print(f"    - {name}")
    if summary["failed_names"]:
        print("  failed:")
        for name in summary["failed_names"]:
            print(f"    - {name}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve Lingowhale channels and add them to the sources registry."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--resolve", nargs="+", metavar="名字")
    mode.add_argument("--apply", nargs="+", metavar="名字")
    mode.add_argument("--apply-id", metavar="channel_id")
    parser.add_argument("--name", metavar="名字")
    args = parser.parse_args(argv)
    if args.apply_id and not args.name:
        parser.error("--apply-id requires --name")
    if args.name and not args.apply_id:
        parser.error("--name can only be used with --apply-id")
    return args


def main(argv=None):
    args = parse_args(argv)
    summary = _empty_summary()

    if args.resolve:
        _resolve_inputs(args.resolve, summary)
    elif args.apply:
        records = _resolve_inputs(args.apply, summary)
        _apply_resolved(records, summary)
    else:
        summary["resolved"] = 1
        print(f"MANUAL name={args.name} channel_id={args.apply_id}")
        _apply_resolved(
            [(args.name, {"channel_id": args.apply_id, "name": args.name})],
            summary,
        )

    print_summary(summary)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
