#!/usr/bin/env python3
"""Recompute normalized time bounds for clusters touched by recent docs."""
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
from clustering import pipeline


def _cluster_ids(conn, fetched_since: str, explicit: str) -> list[int]:
    if explicit:
        return [int(x.strip()) for x in explicit.split(',') if x.strip()]
    rows = conn.execute(
        """SELECT DISTINCT cluster_id
           FROM items
           WHERE fetched_at >= ?
             AND cluster_id IS NOT NULL
           ORDER BY cluster_id""",
        (fetched_since,),
    ).fetchall()
    return [int(r['cluster_id']) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description='Normalize cluster time bounds')
    parser.add_argument('--fetched-since', default='2026-04-26')
    parser.add_argument('--cluster-ids', default='')
    parser.add_argument('--tau-hours', type=float, default=24.0)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    conn = db.get_conn()
    try:
        cids = _cluster_ids(conn, args.fetched_since, args.cluster_ids)
        if not args.apply:
            print(json.dumps({
                'apply': False,
                'fetched_since': args.fetched_since,
                'clusters': len(cids),
                'sample_cluster_ids': cids[:20],
            }, ensure_ascii=False))
            return 0
        for cid in cids:
            pipeline._finalize_cluster_state(conn, cid, tau_hours=args.tau_hours)
        conn.commit()
        print(json.dumps({
            'apply': True,
            'fetched_since': args.fetched_since,
            'clusters_normalized': len(cids),
        }, ensure_ascii=False))
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
