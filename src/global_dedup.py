#!/usr/bin/env python3
"""Global dedup: compare all pending actions against each other using semantic dedup.

Strategy: sort by title so similar actions are adjacent, then use a sliding window
to compare each batch of "new" actions against a window of "existing" actions.
"""
import sys, os, json, sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'src'))

from generate_actions import (
    get_existing_pending_actions, _build_dedup_prompt, _parse_dedup_response,
    call_minimax
)

DB_PATH = os.path.join(BASE, 'data', 'feed.db')
CONFIG_PATH = os.path.join(BASE, 'config', 'config.json')

WINDOW_SIZE = 50   # existing actions to compare against
BATCH_SIZE = 15    # new actions per LLM call


def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    ai = cfg.get('ai_summary', {})
    return ai.get('api_key'), ai.get('api_base'), ai.get('model')


def merge_action(conn, keep, remove, reason=''):
    """Merge `remove` into `keep`: combine source_item_ids, append prompt, delete remove."""
    # Merge source_item_ids
    keep_src = keep.get('source_item_ids', []) or []
    if isinstance(keep_src, str):
        try: keep_src = json.loads(keep_src)
        except: keep_src = []
    rm_src = remove.get('source_item_ids', []) or []
    if isinstance(rm_src, str):
        try: rm_src = json.loads(rm_src)
        except: rm_src = []
    merged_src = list(dict.fromkeys(keep_src + rm_src))

    # Append prompt
    keep_prompt = keep.get('prompt', '') or ''
    rm_prompt = remove.get('prompt', '') or ''
    if rm_prompt and rm_prompt not in keep_prompt:
        updated_prompt = keep_prompt.rstrip() + f"\n\n[补充信息源] {rm_prompt}"
    else:
        updated_prompt = keep_prompt

    conn.execute(
        "UPDATE actions SET source_item_ids=?, prompt=? WHERE id=?",
        (json.dumps(merged_src, ensure_ascii=False), updated_prompt, keep['id'])
    )
    conn.execute("DELETE FROM actions WHERE id=?", (remove['id'],))
    # Update in-memory keep object
    keep['source_item_ids'] = merged_src
    keep['prompt'] = updated_prompt


def global_dedup():
    api_key, api_base, model = load_config()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    all_actions = get_existing_pending_actions(conn)
    print(f"[info] Total pending actions: {len(all_actions)}", flush=True)
    if len(all_actions) < 2:
        print("[info] Nothing to dedup")
        return

    # Sort by title so similar actions are adjacent
    all_actions.sort(key=lambda a: (a.get('title') or '').lower())
    print(f"[info] Sorted by title. Processing in sliding window (window={WINDOW_SIZE}, batch={BATCH_SIZE})", flush=True)

    merged_ids = set()
    total_merged = 0

    # Sliding window: for each batch at position i, compare against the preceding WINDOW_SIZE actions
    for batch_start in range(BATCH_SIZE, len(all_actions), BATCH_SIZE):
        # New actions = current batch
        new_actions = [a for a in all_actions[batch_start:batch_start + BATCH_SIZE]
                       if a['id'] not in merged_ids]
        if not new_actions:
            continue

        # Existing = preceding window (not merged)
        window_start = max(0, batch_start - WINDOW_SIZE)
        existing = [a for a in all_actions[window_start:batch_start]
                    if a['id'] not in merged_ids]
        if not existing:
            continue

        batch_num = batch_start // BATCH_SIZE
        try:
            prompt_content = _build_dedup_prompt(existing, new_actions)
            result = call_minimax(
                api_key, api_base, model,
                "你是行动点去重引擎，严格输出 JSON。",
                prompt_content,
                max_tokens=4096
            )
            parsed = _parse_dedup_response(result, len(new_actions))
            if parsed is None:
                print(f"  Batch {batch_num}: parse failed, skipping", flush=True)
                continue

            merge_list, _ = parsed
            batch_merged = 0

            for m in merge_list:
                if not isinstance(m, dict):
                    continue
                new_idx = m.get('new_index')
                existing_id = m.get('existing_id')
                if new_idx is None or existing_id is None or new_idx >= len(new_actions):
                    continue

                na = new_actions[new_idx]
                if na['id'] in merged_ids:
                    continue
                ea = next((e for e in existing if e['id'] == existing_id), None)
                if not ea:
                    continue

                merge_action(conn, ea, na, m.get('reason', ''))
                merged_ids.add(na['id'])
                batch_merged += 1
                print(f"    MERGE: \"{na.get('title','')}\" → \"{ea.get('title','')}\" ({m.get('reason','')})", flush=True)

            if batch_merged > 0:
                conn.commit()
                total_merged += batch_merged
                print(f"  Batch {batch_num}: merged {batch_merged}", flush=True)
            else:
                print(f"  Batch {batch_num}: no duplicates ({len(new_actions)} vs {len(existing)})", flush=True)

        except Exception as e:
            print(f"  Batch {batch_num}: error — {e}", flush=True)
            continue

    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM actions WHERE status IN ('pending','confirmed')").fetchone()[0]
    print(f"\n[done] Merged {total_merged} duplicate actions. {len(all_actions)} → {remaining}", flush=True)
    conn.close()


if __name__ == '__main__':
    global_dedup()
