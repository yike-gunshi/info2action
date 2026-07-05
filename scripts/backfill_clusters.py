"""Backfill cluster_id for items that haven't been processed yet.

Selects items with cluster_id IS NULL within the last N days, runs them through
the pipeline in small serial batches with checkpointing.

Checkpoint file: data/backfill_v15_progress.json
  {last_processed_id, total_processed, started_at, batch_count, days}

Resume: re-run with --resume; the script picks up after `last_processed_id`.

Dry-run: --dry-run only counts + prints planned batches. No embedding calls,
no DB writes.

Usage:
    python3 scripts/backfill_clusters.py --days 7 --dry-run
    python3 scripts/backfill_clusters.py --days 7
    python3 scripts/backfill_clusters.py --days 2 --resume
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import db as db_mod  # noqa: E402
from clustering import embedding_provider as ep_mod  # noqa: E402
from clustering import pipeline as pipeline_mod  # noqa: E402

DEFAULT_PROGRESS_PATH = ROOT / 'data' / 'backfill_v15_progress.json'
DEFAULT_BATCH = 10
INTER_BATCH_SLEEP_SEC = 1.0


def _load_progress(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_progress(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def select_pending(conn: sqlite3.Connection, *, days: int,
                   after_id: str | None) -> list[sqlite3.Row]:
    if after_id:
        rows = conn.execute(
            """SELECT id FROM items
               WHERE cluster_id IS NULL
                 AND COALESCE(published_at, fetched_at) > datetime('now', ?)
                 AND id > ?
               ORDER BY id ASC""",
            (f'-{int(days)} days', after_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id FROM items
               WHERE cluster_id IS NULL
                 AND COALESCE(published_at, fetched_at) > datetime('now', ?)
               ORDER BY id ASC""",
            (f'-{int(days)} days',),
        ).fetchall()
    return rows


def chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run_backfill(
    conn: sqlite3.Connection,
    *,
    days: int,
    progress_path: Path,
    dry_run: bool = False,
    resume: bool = False,
    batch_size: int = DEFAULT_BATCH,
    sleep_sec: float = INTER_BATCH_SLEEP_SEC,
    progress_every: int = 100,
) -> dict:
    state = _load_progress(progress_path) if resume else {}
    after_id = state.get('last_processed_id') if resume else None
    started_at = state.get('started_at') if resume else datetime.now(timezone.utc).isoformat()

    pending = select_pending(conn, days=days, after_id=after_id)
    print(f"[backfill] {len(pending)} items pending (days={days}, after_id={after_id})",
          flush=True)
    if not pending:
        print("[backfill] nothing to do", flush=True)
        return {'pending': 0, 'processed': 0, 'dry_run': dry_run}

    if dry_run:
        batch_count = 0
        for batch in chunked(pending, batch_size):
            batch_count += 1
            if batch_count <= 5 or batch_count == 1:
                print(f"[backfill] dry-run batch #{batch_count}: {len(batch)} items "
                      f"(first={batch[0]['id']!r} last={batch[-1]['id']!r})", flush=True)
        print(f"[backfill] dry-run total batches: {batch_count}", flush=True)
        return {'pending': len(pending), 'processed': 0, 'dry_run': True,
                'batches_planned': batch_count}

    # Real run: load provider + LLM judge
    cfg = pipeline_mod._load_cfg()
    name, api_key, _ = ep_mod.resolve_runtime_provider(cfg)
    try:
        provider = ep_mod.get_provider(name, api_key=api_key)
    except RuntimeError as e:
        print(f"[backfill] provider init failed: {e}", flush=True)
        print(f"[backfill] (see backlog BF-0424-EMB-KEY)", flush=True)
        return {'pending': len(pending), 'processed': 0, 'error': str(e)}

    ai = cfg.get('ai_summary', {})
    api_base = ai.get('api_base')
    model = ai.get('model', 'MiniMax-M2.7')

    state.setdefault('started_at', started_at)
    state.setdefault('total_processed', 0)
    state.setdefault('batch_count', 0)
    state['days'] = days

    # Save progress on interrupt
    def _sigint(signum, frame):
        print(f"\n[backfill] interrupted — saving progress to {progress_path}", flush=True)
        _save_progress(progress_path, state)
        sys.exit(130)
    signal.signal(signal.SIGINT, _sigint)

    processed = 0
    last_id = after_id
    last_progress_print = 0
    for batch in chunked(pending, batch_size):
        ids = [r['id'] for r in batch]
        try:
            stats = pipeline_mod.run_pipeline(
                conn, provider=provider,
                llm_judge=lambda a, b, scenario: pipeline_mod._default_llm_judge(
                    a, b, scenario=scenario,
                    api_key=api_key, api_base=api_base, model=model,
                ),
                api_key=api_key, api_base=api_base, model=model,
            )
        except Exception as e:
            print(f"[backfill] batch failed (ids={ids[:3]}…): {e}", flush=True)
            time.sleep(sleep_sec * 2)
            continue
        processed += len(batch)
        state['total_processed'] += len(batch)
        state['batch_count'] += 1
        state['last_processed_id'] = ids[-1]
        last_id = ids[-1]
        if processed - last_progress_print >= progress_every:
            print(f"[backfill] progress: {processed}/{len(pending)} processed "
                  f"(stats={stats})", flush=True)
            _save_progress(progress_path, state)
            last_progress_print = processed
        time.sleep(sleep_sec)

    _save_progress(progress_path, state)
    print(f"[backfill] done: processed={processed}/{len(pending)}", flush=True)
    return {'pending': len(pending), 'processed': processed,
            'last_id': last_id, 'state': state}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=7, help='look-back window in days')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--resume', action='store_true',
                        help='continue from previous progress checkpoint')
    parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH)
    parser.add_argument('--sleep-sec', type=float, default=INTER_BATCH_SLEEP_SEC)
    parser.add_argument('--progress-path', default=str(DEFAULT_PROGRESS_PATH))
    args = parser.parse_args(argv)

    conn = db_mod.get_conn()
    try:
        result = run_backfill(
            conn, days=args.days, dry_run=args.dry_run, resume=args.resume,
            batch_size=args.batch_size, sleep_sec=args.sleep_sec,
            progress_path=Path(args.progress_path),
        )
    finally:
        conn.close()
    if result.get('error'):
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
