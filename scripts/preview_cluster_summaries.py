#!/usr/bin/env python3
"""Preview or apply AI summaries for a small set of clusters.

Default mode is dry-run: call the LLM, parse the event/non-event decision, and
print JSON without touching the DB. Use --apply only after reviewing the sample.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import db
import enrich_items
from clustering import summary_writer


def _load_cfg() -> dict:
    with open(os.path.join(BASE_DIR, 'config', 'config.json'), 'r') as f:
        return json.load(f)


def _parse_cluster_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in (value or '').split(','):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


def _candidate_cluster_ids(conn, fetched_since: str, limit: int) -> list[int]:
    rows = conn.execute(
        """SELECT c.id, COUNT(*) AS today_docs
           FROM clusters c
           JOIN cluster_items ci ON ci.cluster_id = c.id
           JOIN items i ON i.id = ci.item_id
           WHERE i.fetched_at >= ?
             AND c.doc_count >= 2
           GROUP BY c.id
           ORDER BY today_docs DESC, c.last_doc_at DESC, c.id DESC
           LIMIT ?""",
        (fetched_since, limit),
    ).fetchall()
    return [int(r['id']) for r in rows]


def _member_titles(conn, cluster_id: int, limit: int = 5) -> list[str]:
    rows = conn.execute(
        """SELECT i.title
           FROM items i JOIN cluster_items ci ON ci.item_id = i.id
           WHERE ci.cluster_id = ?
           ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
           LIMIT ?""",
        (cluster_id, limit),
    ).fetchall()
    return [(r['title'] or '').strip() for r in rows if (r['title'] or '').strip()]


def _preview_one(conn, cluster_id: int, *, api_key: str, api_base: str | None,
                 model: str, summary_max_docs: int) -> dict:
    segs = summary_writer._collect_member_docs(conn, cluster_id, summary_max_docs)
    if not segs:
        return {'cluster_id': cluster_id, 'ok': False, 'error': 'no_members'}
    user_content = '\n\n---\n\n'.join(segs)
    system_prompt = summary_writer.load_prompt(
        '07_cluster_summary.md',
        cluster_docs=user_content,
    ) or user_content
    raw = summary_writer._call_llm_chat(
        api_key=api_key,
        api_base=api_base,
        model=model,
        system_prompt=system_prompt,
        user_content=user_content,
        max_tokens=2048,
    )
    parsed = summary_writer._parse_llm_json(raw)
    cluster = conn.execute(
        """SELECT id, ai_title, ai_summary, doc_count, first_doc_at, last_doc_at,
                  is_visible_in_feed
           FROM clusters WHERE id = ?""",
        (cluster_id,),
    ).fetchone()
    return {
        'cluster_id': cluster_id,
        'ok': bool(parsed),
        'current': dict(cluster) if cluster else None,
        'preview': parsed,
        'member_titles': _member_titles(conn, cluster_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Preview cluster AI summaries')
    parser.add_argument('--cluster-ids', default='', help='Comma-separated cluster ids')
    parser.add_argument('--fetched-since', default='2026-04-26')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--summary-max-docs', type=int, default=20)
    parser.add_argument('--apply', action='store_true',
                        help='Write summaries to DB using regenerate_and_swap')
    args = parser.parse_args()

    cfg = _load_cfg()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(
        cfg.get('ai_summary', {})
    )
    if not api_key:
        print('ERROR: missing MiniMax API key in env/.env/config', file=sys.stderr)
        return 2

    conn = db.get_conn()
    try:
        cluster_ids = _parse_cluster_ids(args.cluster_ids)
        if not cluster_ids:
            cluster_ids = _candidate_cluster_ids(conn, args.fetched_since, args.limit)

        for cid in cluster_ids[:args.limit]:
            if args.apply:
                ok = summary_writer.regenerate_and_swap(
                    conn,
                    cid,
                    api_key=api_key,
                    api_base=api_base,
                    model=model,
                    summary_max_docs=args.summary_max_docs,
                )
                conn.commit()
                print(json.dumps({'cluster_id': cid, 'applied': ok}, ensure_ascii=False))
            else:
                print(json.dumps(
                    _preview_one(
                        conn,
                        cid,
                        api_key=api_key,
                        api_base=api_base,
                        model=model,
                        summary_max_docs=args.summary_max_docs,
                    ),
                    ensure_ascii=False,
                ))
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
