#!/usr/bin/env python3
"""
Direction-aware action dedup engine (v2).

Groups actions by direction, then deduplicates within each direction:
  Stage 1: TF-IDF vector clustering (zero API calls)
  Stage 2: LLM semantic confirmation (only on candidate clusters)

Usage:
  python3 dedup_actions.py --dry-run        # preview clusters, save plan
  python3 dedup_actions.py --apply          # run both stages and apply
  python3 dedup_actions.py --apply-plan     # apply saved plan (no LLM)
  python3 dedup_actions.py --threshold 0.3  # adjust TF-IDF similarity threshold
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime

import jieba
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
DIRECTIONS_PATH = os.path.join(BASE_DIR, "config", "directions.yaml")


def _safe_log(conn, action_id, event_type, details):
    """Log action event, silently skip if table doesn't exist."""
    try:
        db._log_action_event(conn, action_id, event_type, details)
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_directions():
    """Load directions framework. Returns dict of slug → {label, description}."""
    try:
        import yaml
        with open(DIRECTIONS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get('directions', {})
    except ImportError:
        # Fallback: basic parsing
        dirs = {}
        try:
            with open(DIRECTIONS_PATH, "r", encoding="utf-8") as f:
                raw = f.read()
            current = None
            for line in raw.split('\n'):
                m = re.match(r'^  (\w[\w-]*):', line)
                if m:
                    current = m.group(1)
                    dirs[current] = {}
                elif current:
                    m2 = re.match(r'^\s+label:\s*"?([^"]+)"?', line)
                    if m2:
                        dirs[current]['label'] = m2.group(1).strip()
                    m3 = re.match(r'^\s+description:\s*"?([^"]+)"?', line)
                    if m3:
                        dirs[current]['description'] = m3.group(1).strip()
        except Exception:
            pass
        return dirs
    except Exception:
        return {}


def call_minimax(api_key, api_base, model, system_msg, user_msg, max_tokens=4096):
    """Call MiniMax API via Anthropic-compatible endpoint."""
    url = f"{api_base}/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system_msg,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode("utf-8")

    ctx = ssl.create_default_context()

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with ai_provider_guard.guarded_urlopen(
            req,
            source="dedup_actions",
            context=ctx,
            timeout=120,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content", [])
        if content and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
            return content[0].get("text", "")
        return ""
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ai_provider_guard.ensure_provider_available("minimax", source="dedup_actions")
        raise


# ── Stage 1: TF-IDF Vector Clustering ──

def tokenize_chinese(text):
    """Segment Chinese text + keep English words."""
    words = jieba.cut(text, cut_all=False)
    return " ".join(w.strip() for w in words if w.strip())


def build_action_text(action):
    """Combine title + prompt + reason into a single searchable text."""
    parts = [action.get('title', '') or '', action.get('prompt', '') or '']
    reason = action.get('reason', '') or ''
    if isinstance(reason, (dict, list)):
        reason = json.dumps(reason, ensure_ascii=False)
    parts.append(reason[:500])
    return ' '.join(parts)


def find_candidate_clusters(actions, threshold=0.25):
    """TF-IDF + cosine similarity → clusters of similar actions."""
    if len(actions) < 2:
        return []

    texts = [tokenize_chinese(build_action_text(a)) for a in actions]
    vectorizer = TfidfVectorizer(max_features=5000, min_df=1)
    tfidf_matrix = vectorizer.fit_transform(texts)
    sim_matrix = cosine_similarity(tfidf_matrix)

    n = len(actions)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if sim_matrix[i][j] >= threshold:
                pairs.append((i, j, sim_matrix[i][j]))

    if not pairs:
        return []

    # Union-Find
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i, j, _ in pairs:
        union(i, j)

    clusters = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    return [idxs for idxs in clusters.values() if len(idxs) >= 2]


# ── Stage 2: LLM Semantic Confirmation (direction-aware) ──

def build_dedup_prompt(direction_slug, direction_info, actions_in_direction):
    """Build direction-aware dedup prompt."""
    from prompt_loader import load_prompt

    direction_label = direction_info.get('label', direction_slug) if direction_info else direction_slug
    direction_desc = direction_info.get('description', '') if direction_info else ''

    actions_text_parts = []
    for i, a in enumerate(actions_in_direction):
        reason = a.get('reason', '') or ''
        if isinstance(reason, (dict, list)):
            reason = json.dumps(reason, ensure_ascii=False)
        actions_text_parts.append(
            f"[{i}] 标题: {a.get('title', '')}\n"
            f"    执行: {(a.get('prompt', '') or '')[:300]}\n"
            f"    原因: {reason[:200]}"
        )
    actions_text = "\n\n".join(actions_text_parts)

    # Try to load from prompt template
    prompt = load_prompt('04b_action_dedup.md',
                         direction_label=direction_label,
                         direction_description=direction_desc,
                         actions_in_direction=actions_text)
    if prompt:
        return prompt

    # Fallback inline prompt
    return f"""你是行动点聚合引擎。以下行动点都属于【{direction_label}】方向（{direction_desc}）。

## 合并规则
- **同一目标方向的不同阶段** → 合并。步骤按递进排列（调研→评估→集成）
  - 例：「调研 WeClaw 技术方案」+「评估 WeClaw 接入可行性」→ 合并
- **同一实体但不同目标** → 保持独立
  - 例：「修复 CVE 漏洞」和「调研微信集成」→ 不合并
- **同类别的泛调研** → 合并为一个选型行动
  - 例：「调研 mem9」+「调研 Signet」+「调研 Memento Vault」→ 合并为「记忆系统选型」

## 候选行动点（方向：{direction_label}）
{actions_text}

## 输出格式
严格 JSON，不要 markdown 代码块：
{{"groups": [{{"action_indices": [0, 2], "merged_title": "合并后标题", "merged_prompt": "1. 步骤一\\n2. 步骤二", "merge_reason": "原因"}}], "independent": [1, 3]}}

每个索引必须恰好出现一次。不确定时倾向保持独立。"""


def confirm_cluster_with_llm(actions_in_cluster, direction_slug, direction_info, api_key, api_base, model):
    """Ask LLM to confirm merges within a direction cluster."""
    user_msg = build_dedup_prompt(direction_slug, direction_info, actions_in_cluster)

    result = call_minimax(
        api_key, api_base, model,
        "你是行动点聚合引擎，严格输出 JSON。",
        user_msg,
        max_tokens=4096,
    )

    if not result:
        return None

    text = result.strip()
    if text.startswith('```'):
        text = re.sub(r'^```\w*\n?', '', text)
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return None
        else:
            return None

    groups = data.get('groups', [])
    independent = data.get('independent', [])

    # Normalize all indices to int (LLM sometimes returns strings)
    def _to_int(x):
        try: return int(x)
        except (ValueError, TypeError): return None

    for g in groups:
        g['action_indices'] = [x for x in (_to_int(i) for i in g.get('action_indices', [])) if x is not None]
    independent = [x for x in (_to_int(i) for i in independent) if x is not None]

    # Validate coverage
    n = len(actions_in_cluster)
    covered = set()
    for g in groups:
        covered.update(g['action_indices'])
    covered.update(independent)
    missing = set(range(n)) - covered
    if missing:
        independent = list(set(independent) | missing)

    return groups, independent


# ── Orchestrator ──

def run_dedup(dry_run=True, threshold=0.25, max_cluster_size=20):
    """Main: group by direction → TF-IDF within direction → LLM confirm → merge."""
    cfg = load_config()
    ai = cfg.get('ai_summary', {})
    # oss-release F3c: env/.env 优先（对齐 resolve_minimax_*_config），config.json 只留空模板
    from env_utils import load_project_env
    project_env = load_project_env(BASE_DIR)
    api_key = (
        os.environ.get('MINIMAX_API_KEY')
        or project_env.get('MINIMAX_API_KEY')
        or ai.get('api_key', '')
        or ''
    ).strip()
    api_base = (
        os.environ.get('MINIMAX_API_BASE')
        or project_env.get('MINIMAX_API_BASE')
        or ai.get('api_base', '')
        or 'https://api.minimaxi.com/anthropic/v1'
    ).strip()
    model = ai.get('model', 'MiniMax-M3')
    provider = ai.get('provider', 'minimax')

    if provider == "minimax":
        try:
            ai_provider_guard.ensure_provider_available("minimax", source="dedup_actions.run_dedup")
        except ai_provider_guard.ProviderCooldown as e:
            print(f"[info] MiniMax cooldown active, skipping action dedup until {e.cooldown_until}")
            print(f"[info] {ai_provider_guard.cooldown_message('minimax')}")
            return

    directions = load_directions()
    conn = db.get_conn()

    # Load pending actions with direction
    rows = conn.execute(
        "SELECT id, title, prompt, reason, source_item_ids, priority, action_type, "
        "related_project, direction, direction_label "
        "FROM actions WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()

    actions = []
    for r in rows:
        src = []
        if r[4]:
            try:
                src = json.loads(r[4])
            except (json.JSONDecodeError, TypeError):
                pass
        actions.append({
            'id': r[0], 'title': r[1], 'prompt': r[2], 'reason': r[3],
            'source_item_ids': src, 'priority': r[5], 'action_type': r[6],
            'related_project': r[7], 'direction': r[8] or '_uncategorized',
            'direction_label': r[9] or '待归类',
        })

    # Group by direction
    by_direction = defaultdict(list)
    for a in actions:
        by_direction[a['direction']].append(a)

    print(f"[info] {len(actions)} pending actions across {len(by_direction)} directions")
    for d, acts in sorted(by_direction.items(), key=lambda x: -len(x[1])):
        label = directions.get(d, {}).get('label', d) if directions else d
        print(f"  {label} ({d}): {len(acts)} actions")

    total_merge_groups = []

    for direction_slug, dir_actions in by_direction.items():
        if len(dir_actions) < 2:
            continue

        direction_info = directions.get(direction_slug, {})
        label = direction_info.get('label', direction_slug) if direction_info else direction_slug

        print(f"\n{'='*60}")
        print(f"Direction: {label} ({len(dir_actions)} actions)")

        # Stage 1: TF-IDF within this direction
        clusters = find_candidate_clusters(dir_actions, threshold=threshold)
        if not clusters:
            print(f"  → No similar pairs found (TF-IDF)")
            continue

        print(f"  → {len(clusters)} candidate clusters")

        for ci, cluster_indices in enumerate(clusters):
            cluster_actions = [dir_actions[i] for i in cluster_indices]
            print(f"\n  Cluster {ci+1} ({len(cluster_indices)} actions):")
            for i, idx in enumerate(cluster_indices):
                a = dir_actions[idx]
                print(f"    [{i}] {a['title']}")

            if len(cluster_actions) > max_cluster_size:
                print(f"    → [skip] too large ({len(cluster_actions)} > {max_cluster_size})")
                continue

            if not api_key:
                # 无 key 时干净跳过 LLM 精判（对齐 generate_summaries），
                # 而非带空 key 调用撞 401；dedup 降级为仅 TF-IDF 分组不合并。
                print("    → [no API key] skipping LLM confirmation")
                continue

            # Rate limit
            if ci > 0 or len(total_merge_groups) > 0:
                time.sleep(2)

            # Stage 2: LLM confirmation
            print(f"    → Asking LLM...")
            try:
                result = confirm_cluster_with_llm(
                    cluster_actions, direction_slug, direction_info,
                    api_key, api_base, model
                )
            except ai_provider_guard.ProviderCooldown as e:
                print(f"    → [cooldown] MiniMax cooldown active until {e.cooldown_until}")
                conn.close()
                return
            if result is None:
                print(f"    → [warn] LLM failed, keeping all independent")
                continue

            groups, independent = result

            for g in groups:
                indices = g.get('action_indices', [])
                if len(indices) < 2:
                    continue
                real_ids = [cluster_actions[i]['id'] for i in indices if i < len(cluster_actions)]
                real_titles = [cluster_actions[i]['title'] for i in indices if i < len(cluster_actions)]
                print(f"    ✓ MERGE: {real_titles}")
                print(f"      → {g.get('merged_title', '?')}")
                total_merge_groups.append({
                    'action_ids': real_ids,
                    'actions': [cluster_actions[i] for i in indices if i < len(cluster_actions)],
                    'merged_title': g.get('merged_title', ''),
                    'merged_prompt': g.get('merged_prompt', ''),
                    'merge_reason': g.get('merge_reason', ''),
                    'direction': direction_slug,
                    'direction_label': label,
                })

            for i in independent:
                try:
                    idx = int(i)
                    if 0 <= idx < len(cluster_actions):
                        print(f"    ○ KEEP: {cluster_actions[idx]['title']}")
                except (ValueError, TypeError):
                    pass

    print(f"\n{'='*60}")
    print(f"Summary: {len(total_merge_groups)} merge groups")

    # Save plan
    plan_path = os.path.join(BASE_DIR, "data", "dedup_plan.json")
    plan_data = [{
        'action_ids': mg['action_ids'],
        'merged_title': mg['merged_title'],
        'merged_prompt': mg['merged_prompt'],
        'merge_reason': mg['merge_reason'],
        'direction': mg.get('direction', '_uncategorized'),
    } for mg in total_merge_groups]
    with open(plan_path, 'w') as f:
        json.dump(plan_data, f, ensure_ascii=False, indent=2)
    print(f"[saved] Plan → {plan_path}")

    if dry_run:
        print("[dry-run] Use --apply-plan to apply.")
        conn.close()
        return

    # Apply merges
    _apply_merges(conn, total_merge_groups)
    conn.close()


def _apply_merges(conn, merge_groups):
    """Apply merge groups to DB."""
    merged_count = 0
    deleted_count = 0

    for mg in merge_groups:
        ids = mg['action_ids']
        if len(ids) < 2:
            continue

        survivor_id = ids[0]
        victim_ids = ids[1:]

        # Collect all source_item_ids
        all_src_ids = []
        for a in mg.get('actions', []):
            all_src_ids.extend(a.get('source_item_ids', []) or [])
        all_src_ids = list(dict.fromkeys(all_src_ids))

        # Highest priority
        priority_order = {'bug': 0, 'high': 1, 'medium': 2, 'low': 3}
        best = min(
            (priority_order.get(a.get('priority', 'medium'), 2) for a in mg.get('actions', [])),
            default=2
        )
        best_name = {v: k for k, v in priority_order.items()}[best]

        db.update_action(conn, survivor_id,
                         title=mg['merged_title'],
                         prompt=mg['merged_prompt'],
                         source_item_ids=json.dumps(all_src_ids, ensure_ascii=False),
                         priority=best_name)

        _safe_log(conn, survivor_id, 'merged', {
            'absorbed_ids': victim_ids,
            'merge_reason': mg['merge_reason'],
        })

        for vid in victim_ids:
            conn.execute(
                "UPDATE actions SET status='dismissed', dismissed_at=datetime('now') WHERE id=?",
                (vid,)
            )
            _safe_log(conn, vid, 'dismissed', {
                'reason': f"Merged into {survivor_id}",
            })
            deleted_count += 1

        merged_count += 1
        print(f"  ✓ {mg['merged_title']} (absorbed {len(victim_ids)})")

    conn.commit()
    print(f"\n[done] {merged_count} merges, {deleted_count} absorbed")


def apply_saved_plan():
    """Apply a previously saved merge plan without re-calling LLM."""
    plan_path = os.path.join(BASE_DIR, "data", "dedup_plan.json")
    if not os.path.exists(plan_path):
        print(f"[error] No saved plan at {plan_path}. Run dry-run first.")
        return

    with open(plan_path, 'r') as f:
        plan_data = json.load(f)

    if not plan_data:
        print("[info] Plan is empty.")
        return

    print(f"[info] Applying {len(plan_data)} merge groups...")
    conn = db.get_conn()

    rows = conn.execute(
        "SELECT id, title, source_item_ids, priority FROM actions WHERE status='pending'"
    ).fetchall()
    action_map = {}
    for r in rows:
        src = []
        if r[2]:
            try:
                src = json.loads(r[2])
            except (json.JSONDecodeError, TypeError):
                pass
        action_map[r[0]] = {'id': r[0], 'title': r[1], 'source_item_ids': src, 'priority': r[3]}

    merge_groups = []
    for mg in plan_data:
        ids = mg['action_ids']
        valid = [aid for aid in ids if aid in action_map]
        if len(valid) < 2:
            print(f"  [skip] {mg['merged_title']}")
            continue
        merge_groups.append({
            'action_ids': valid,
            'actions': [action_map[aid] for aid in valid],
            'merged_title': mg['merged_title'],
            'merged_prompt': mg['merged_prompt'],
            'merge_reason': mg['merge_reason'],
        })

    _apply_merges(conn, merge_groups)
    conn.close()

    archive_path = plan_path + f".applied-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    os.rename(plan_path, archive_path)
    print(f"[archived] → {archive_path}")


def main():
    parser = argparse.ArgumentParser(description="Direction-aware action dedup (v2)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    parser.add_argument("--apply", action="store_true", help="Run + apply merges")
    parser.add_argument("--apply-plan", action="store_true", help="Apply saved plan (no LLM)")
    parser.add_argument("--threshold", type=float, default=0.25, help="TF-IDF threshold (default 0.25)")
    parser.add_argument("--max-cluster", type=int, default=20, help="Max cluster size (default 20)")
    args = parser.parse_args()

    if args.apply_plan:
        apply_saved_plan()
        return

    if args.apply:
        args.dry_run = False

    run_dedup(dry_run=args.dry_run, threshold=args.threshold, max_cluster_size=args.max_cluster)


if __name__ == '__main__':
    main()
