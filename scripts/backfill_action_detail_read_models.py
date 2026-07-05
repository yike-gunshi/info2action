#!/usr/bin/env python3
"""Backfill display-ready action detail read models."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import action_detail_read_model
import db
import remote_db


def _referenced_urls(detail_json):
    detail = remote_db._json_value(detail_json) if detail_json is not None else {}
    return detail.get("referenced_urls", []) if isinstance(detail, dict) else []


def _ordered_source_items(source_ids, rows_by_id, *, request_user_id, can_view_all):
    out = []
    for source_id in action_detail_read_model.parse_source_item_ids(source_ids):
        item = rows_by_id.get(source_id)
        if not item:
            continue
        if (
            item.get("platform") == "manual"
            and not can_view_all
            and item.get("user_id") != request_user_id
        ):
            continue
        out.append({
            "id": item.get("id"),
            "platform": item.get("platform"),
            "title": item.get("title"),
            "ai_summary": item.get("ai_summary"),
            "url": item.get("url"),
            "referenced_urls": _referenced_urls(item.get("detail_json")),
        })
    return out


def _bulk_upsert_remote_read_models(conn, *, schema, records):
    if not records:
        return
    conn.execute(
        f"""WITH rows AS (
              SELECT *
                FROM jsonb_to_recordset(%s) AS r(
                  action_id text,
                  viewer_scope text,
                  owner_user_id text,
                  payload jsonb,
                  source_item_ids jsonb
                )
            )
            INSERT INTO {schema}.action_detail_read_models
              (action_id, viewer_scope, owner_user_id, payload, source_item_ids,
               payload_version, built_at)
            SELECT action_id, viewer_scope, owner_user_id, payload, source_item_ids,
                   %s, now()
              FROM rows
            ON CONFLICT (action_id, viewer_scope) DO UPDATE SET
              owner_user_id = excluded.owner_user_id,
              payload = excluded.payload,
              source_item_ids = excluded.source_item_ids,
              payload_version = excluded.payload_version,
              built_at = excluded.built_at""",
        (
            remote_db._maybe_jsonb(records),
            action_detail_read_model.READ_MODEL_VERSION,
        ),
    )


def _upsert_local_read_model(conn, *, action_id, viewer_scope, owner_user_id, payload, source_item_ids):
    import json

    conn.execute(
        """
        INSERT INTO action_detail_read_models
          (action_id, viewer_scope, owner_user_id, payload_json, source_item_ids,
           payload_version, built_at)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(action_id, viewer_scope) DO UPDATE SET
          owner_user_id = excluded.owner_user_id,
          payload_json = excluded.payload_json,
          source_item_ids = excluded.source_item_ids,
          payload_version = excluded.payload_version,
          built_at = excluded.built_at
        """,
        (
            action_id,
            viewer_scope,
            owner_user_id,
            json.dumps(payload, ensure_ascii=False, default=action_detail_read_model.json_default),
            json.dumps(source_item_ids or [], ensure_ascii=False),
            action_detail_read_model.READ_MODEL_VERSION,
        ),
    )


def _backfill_remote(limit: int, include_admin_scope: bool, chunk_size: int) -> int:
    schema = remote_db.remote_schema()
    with remote_db.connect() as conn:
        rows = conn.execute(
            f"""SELECT *
                  FROM {schema}.actions
                 ORDER BY created_at DESC
                 LIMIT %s""",
            (limit,),
        ).fetchall()
        actions = [remote_db._normalize_action_row(row) for row in rows]
        source_ids = []
        for action in actions:
            source_ids.extend(action_detail_read_model.parse_source_item_ids(action.get("source_item_ids")))
        source_ids = list(dict.fromkeys(source_ids))
        source_rows = []
        if source_ids:
            placeholders = ", ".join(["%s"] * len(source_ids))
            source_rows = conn.execute(
                f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                      FROM {schema}.items
                     WHERE id IN ({placeholders})""",
                tuple(source_ids),
            ).fetchall()
        rows_by_id = {row["id"]: dict(row) for row in source_rows}
        count = 0
        read_model_records = []
        def flush_records():
            if not read_model_records:
                return
            _bulk_upsert_remote_read_models(conn, schema=schema, records=read_model_records)
            conn.commit()
            read_model_records.clear()

        for action in actions:
            action_id = action["id"]
            owner_user_id = action.get("user_id")
            owner_sources = _ordered_source_items(
                action.get("source_item_ids"),
                rows_by_id,
                request_user_id=owner_user_id,
                can_view_all=False,
            )
            payload = action_detail_read_model.build_action_detail_payload(
                action,
                source_items=owner_sources,
            )
            read_model_records.append({
                "action_id": action_id,
                "viewer_scope": "owner",
                "owner_user_id": owner_user_id,
                "payload": payload,
                "source_item_ids": action.get("source_item_ids") or [],
            })
            count += 1
            if include_admin_scope:
                admin_sources = _ordered_source_items(
                    action.get("source_item_ids"),
                    rows_by_id,
                    request_user_id=None,
                    can_view_all=True,
                )
                admin_payload = action_detail_read_model.build_action_detail_payload(
                    action,
                    source_items=admin_sources,
                )
                read_model_records.append({
                    "action_id": action_id,
                    "viewer_scope": "admin",
                    "owner_user_id": owner_user_id,
                    "payload": admin_payload,
                    "source_item_ids": action.get("source_item_ids") or [],
                })
            if count % chunk_size == 0:
                flush_records()
        flush_records()
        return count


def _backfill_local(limit: int, include_admin_scope: bool, chunk_size: int) -> int:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        actions = []
        for row in rows:
            action = dict(row)
            action["source_item_ids"] = action_detail_read_model.parse_source_item_ids(action.get("source_item_ids"))
            actions.append(action)
        source_ids = []
        for action in actions:
            source_ids.extend(action.get("source_item_ids") or [])
        source_ids = list(dict.fromkeys(source_ids))
        rows_by_id = {}
        if source_ids:
            placeholders = ", ".join(["?"] * len(source_ids))
            source_rows = conn.execute(
                f"""SELECT id, user_id, platform, title, ai_summary, url, detail_json
                      FROM items
                     WHERE id IN ({placeholders})""",
                source_ids,
            ).fetchall()
            rows_by_id = {row["id"]: dict(row) for row in source_rows}
        count = 0
        for action in actions:
            action_id = action["id"]
            owner_user_id = action.get("user_id")
            owner_sources = _ordered_source_items(
                action.get("source_item_ids"),
                rows_by_id,
                request_user_id=owner_user_id,
                can_view_all=False,
            )
            payload = action_detail_read_model.build_action_detail_payload(
                action,
                source_items=owner_sources,
            )
            _upsert_local_read_model(
                conn,
                action_id=action_id,
                viewer_scope="owner",
                owner_user_id=owner_user_id,
                payload=payload,
                source_item_ids=action.get("source_item_ids") or [],
            )
            count += 1
            if include_admin_scope:
                admin_sources = _ordered_source_items(
                    action.get("source_item_ids"),
                    rows_by_id,
                    request_user_id=None,
                    can_view_all=True,
                )
                admin_payload = action_detail_read_model.build_action_detail_payload(
                    action,
                    source_items=admin_sources,
                )
                _upsert_local_read_model(
                    conn,
                    action_id=action_id,
                    viewer_scope="admin",
                    owner_user_id=owner_user_id,
                    payload=admin_payload,
                    source_item_ids=action.get("source_item_ids") or [],
                )
            if count % chunk_size == 0:
                conn.commit()
        conn.commit()
        return count
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000, help="Maximum actions to backfill.")
    parser.add_argument(
        "--backend",
        choices=("auto", "remote", "local"),
        default="auto",
        help="Storage backend to backfill.",
    )
    parser.add_argument(
        "--include-admin-scope",
        action="store_true",
        help="Also build the admin viewer payload variant.",
    )
    parser.add_argument("--chunk-size", type=int, default=100, help="Actions per write batch.")
    args = parser.parse_args()

    backend = args.backend
    if backend == "auto":
        backend = "remote" if remote_db.app_state_to_remote() else "local"

    if backend == "remote":
        count = _backfill_remote(args.limit, args.include_admin_scope, max(1, args.chunk_size))
    else:
        count = _backfill_local(args.limit, args.include_admin_scope, max(1, args.chunk_size))
    print(f"backfilled={count} backend={backend}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
