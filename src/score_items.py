#!/usr/bin/env python3
"""
AI Scoring & Classification for info2action items (v12.0).
Uses MiniMax API to classify items, detect content type, and assign universal quality scores.
No user-profile injection — scoring is user-agnostic.

DEPRECATED v4.0 (2026-04-29):
- 新 item 走 enrich_items.py(v4.0 13 L1 / L2 multi-tag / visible 过滤);
- 文件保留作 helper module(_TYPE_DIMENSIONS / _VALID_CONTENT_TYPES /
  compute_quality_score / compute_weighted_score_legacy 仍被 enrich_items 引用);
- 但顶层 main()/CLI 入口、prompt 模板、scripts/rescore_all.py 均废弃,
  老数据将通过 enrich_items.py 重跑回填,不再调本脚本 main()。

Usage:
  python score_items.py                  # score up to 100 unscored items
  python score_items.py --limit 50       # score up to 50 items
  python score_items.py --force 100      # re-score 100 most recent items (overwrite existing)
  python score_items.py --dry-run        # print scores without writing to DB
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
CLASSIFICATION_PATH = os.path.join(BASE_DIR, "config", "classification.json")
AI_RETRY_READY_SQL = "(ai_retry_after IS NULL OR ai_retry_after <= datetime('now'))"

_SSL_CTX = ssl.create_default_context()


# ── Type-specific quality score formulas ──
# Each content type has different dimensions and weights, normalized to 0-1.
# Dimensions are 1-3 scale from LLM; spam_score is inverted (1=good, 3=bad).

def compute_quality_score(content_type, dimensions):
    """Compute universal quality score (0-1) based on content type and LLM dimensions."""
    def dim(key, default=2):
        return dimensions.get(key, default)

    def norm(val):
        """Normalize 1-3 to 0-1."""
        return (val - 1) / 2.0

    def spam_inv(val):
        """Invert spam_score: 1→1.0, 2→0.5, 3→0.0."""
        return (3 - val) / 2.0

    if content_type == 'flash':
        score = (0.30 * norm(dim('novelty')) +
                 0.25 * norm(dim('info_density')) +
                 0.20 * norm(dim('credibility')) +
                 0.25 * spam_inv(dim('spam_score')))
    elif content_type == 'post':
        score = (0.25 * norm(dim('novelty')) +
                 0.25 * norm(dim('depth')) +
                 0.20 * norm(dim('actionability')) +
                 0.15 * norm(dim('credibility')) +
                 0.15 * spam_inv(dim('spam_score')))
    elif content_type == 'article':
        score = (0.20 * norm(dim('novelty')) +
                 0.30 * norm(dim('depth')) +
                 0.20 * norm(dim('actionability')) +
                 0.15 * norm(dim('credibility')) +
                 0.15 * spam_inv(dim('spam_score')))
    elif content_type == 'video':
        score = (0.30 * norm(dim('novelty')) +
                 0.30 * norm(dim('actionability')) +
                 0.40 * spam_inv(dim('spam_score')))
    elif content_type == 'repo':
        # Repo quality mainly from engagement (star count) — LLM only evaluates novelty
        score = norm(dim('novelty'))
    else:
        # Fallback: average of whatever dimensions we have
        vals = [norm(v) for k, v in dimensions.items() if k != 'spam_score']
        score = sum(vals) / max(len(vals), 1) if vals else 0.5

    return round(max(0.0, min(1.0, score)), 3)


# ── Legacy compatibility: weighted score 1-10 ──

def compute_weighted_score_legacy(category, quality, novelty, depth, relevance=None):
    """Compute legacy weighted score from old 4-dimension format. Returns 1.0-10.0."""
    clf = load_classification()
    scoring = clf.get("scoring", {})
    weights = scoring.get("category_weights", {})
    default_w = {"quality": 0.33, "novelty": 0.34, "depth": 0.33}
    w = weights.get(category, default_w)

    if relevance is not None:
        raw = (quality * w.get("quality", 0.25) +
               novelty * w.get("novelty", 0.25) +
               depth * w.get("depth", 0.25) +
               relevance * w.get("relevance", 0.25))
    else:
        raw = quality * w.get("quality", 0.33) + novelty * w.get("novelty", 0.34) + depth * w.get("depth", 0.33)

    score = 1.0 + (raw - 1.0) * (9.0 / 2.0)
    return round(max(1.0, min(10.0, score)), 1)


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_classification():
    with open(CLASSIFICATION_PATH, "r") as f:
        return json.load(f)


def ensure_ai_columns(conn):
    """Add AI columns if they don't exist."""
    for col in ["ai_category TEXT", "ai_keywords TEXT", "ai_dimensions TEXT",
                 "content_type TEXT", "ai_quality_score REAL",
                 "ai_error_count INTEGER DEFAULT 0", "ai_last_error TEXT",
                 "ai_last_error_at TEXT", "ai_retry_after TEXT"]:
        try:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass  # column already exists


def record_score_failure(item_id, error, retry_after=None, increment=True):
    item_conn = db.get_conn()
    try:
        db.record_ai_failure(item_conn, item_id, error, retry_after=retry_after, increment=increment)
    finally:
        item_conn.close()


def load_recent_feedback(conn, limit=50):
    """Load recent feedback entries to build preference signal."""
    rows = conn.execute("""
        SELECT f.type, f.topic, f.text, i.title
        FROM feedback f
        JOIN items i ON f.item_id = i.id
        ORDER BY f.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def build_feedback_signal(feedback_rows):
    """Build a concise text block describing community preferences from feedback."""
    liked = []
    disliked = []
    for fb in feedback_rows:
        title = (fb.get("title") or "")[:80]
        if not title:
            continue
        if fb["type"] == "positive":
            liked.append(title)
        elif fb["type"] in ("irrelevant", "low_quality"):
            disliked.append(title)

    lines = []
    if liked:
        lines.append("社区正面反馈的内容标题：")
        for t in liked[:15]:
            lines.append(f"  + {t}")
    if disliked:
        lines.append("社区负面反馈的内容标题：")
        for t in disliked[:15]:
            lines.append(f"  - {t}")
    return "\n".join(lines)


def build_system_prompt(categories, feedback_signal):
    """Build the system prompt for classification and scoring (no user profile)."""
    cat_lines = []
    for cat in categories:
        cid = cat["id"]
        name = cat["name"]
        desc = cat["description"]
        rule = cat.get("boundary_rule", "")
        examples = ", ".join(cat.get("examples_in", [])[:5])
        cat_lines.append(f"- {cid}（{name}）：{desc}")
        if rule:
            cat_lines.append(f"  边界规则：{rule}")
        if examples:
            cat_lines.append(f"  典型：{examples}")

    cat_block = "\n".join(cat_lines)

    feedback_block = ""
    if feedback_signal:
        feedback_block = f"""## 社区反馈信号
{feedback_signal}
"""

    from prompt_loader import load_prompt
    prompt = load_prompt('01_classify_and_score.md',
                         categories=cat_block, feedback=feedback_block)
    if prompt:
        return prompt

    # Fallback if file missing
    return f"""你是信息内容分类与评分助手。请对以下内容进行分类和打分。

## 分类体系
{cat_block}

{feedback_block}

## 输出要求
只输出一个JSON对象：
{{"category": "ai_tools", "content_type": "post", "novelty": 2, "credibility": 3, "spam_score": 1, "depth": 2, "actionability": 2, "reason": "简短理由", "keywords": ["Claude Code", "MCP"]}}"""


def call_openai_score(api_key, api_base, model, system_prompt, user_content, max_tokens=1024):
    """Call OpenAI-compatible API for scoring."""
    url = f"{api_base}/chat/completions"
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content[:2000]}
        ]
    }).encode("utf-8")

    for attempt in range(3):
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        })
        try:
            with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
                return ""
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def call_minimax(api_key, api_base, model, system_prompt, user_content, max_tokens=1024):
    """Call MiniMax API (Anthropic-compatible Token Plan)."""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_content[:2000]}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    })
    with ai_provider_guard.guarded_urlopen(
        req,
        source="score_items",
        timeout=30,
        context=_SSL_CTX,
    ) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        for block in result["content"]:
            if block.get("type") == "text":
                return block["text"].strip()
        return ""


def call_llm_score(provider, api_key, api_base, model, system_prompt, user_content, max_tokens=1024):
    """Dispatch to the correct LLM provider for scoring."""
    if provider == 'openai':
        return call_openai_score(api_key, api_base, model, system_prompt, user_content, max_tokens)
    else:
        return call_minimax(api_key, api_base, model, system_prompt, user_content, max_tokens)


# Valid content types
_VALID_CONTENT_TYPES = frozenset(('flash', 'post', 'article', 'video', 'repo'))

# Expected dimensions per content type
_TYPE_DIMENSIONS = {
    'flash': {'novelty', 'credibility', 'spam_score', 'info_density'},
    'post': {'novelty', 'credibility', 'spam_score', 'depth', 'actionability'},
    'article': {'novelty', 'credibility', 'spam_score', 'depth', 'actionability'},
    'video': {'novelty', 'spam_score', 'actionability'},
    'repo': {'novelty'},
}


def parse_score_response(raw, valid_category_ids):
    """Parse JSON response from AI.
    Returns (category, content_type, quality_score, reason, keywords, dimensions) or None.
    """
    json_match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
    if not json_match:
        return None

    try:
        obj = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None

    category = obj.get("category", "").strip().lower()
    reason = obj.get("reason", "")
    keywords = obj.get("keywords", [])
    content_type = obj.get("content_type", "").strip().lower()

    # Validate category
    if category not in valid_category_ids:
        return None

    # Validate content type
    if content_type not in _VALID_CONTENT_TYPES:
        content_type = "post"  # default fallback

    # Parse dimensions based on content type
    expected_dims = _TYPE_DIMENSIONS.get(content_type, set())
    dimensions = {}
    for dim_name in expected_dims:
        val = obj.get(dim_name)
        if val is not None:
            try:
                dimensions[dim_name] = max(1, min(3, int(val)))
            except (TypeError, ValueError):
                dimensions[dim_name] = 2  # default

    # Compute universal quality score
    quality_score = compute_quality_score(content_type, dimensions)

    # Also compute legacy score for backward compatibility (using available dimensions)
    novelty = dimensions.get('novelty', 2)
    depth = dimensions.get('depth', 2)
    credibility = dimensions.get('credibility', 2)
    legacy_score = compute_weighted_score_legacy(category, credibility, novelty, depth)

    # Validate keywords
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k).strip() for k in keywords if k and len(str(k).strip()) > 1][:5]

    return (category, content_type, quality_score, legacy_score, reason, keywords, dimensions)


def format_metrics(metrics_json, platform):
    """Format engagement metrics into readable string."""
    if not metrics_json:
        return None
    try:
        m = json.loads(metrics_json) if isinstance(metrics_json, str) else metrics_json
    except (json.JSONDecodeError, TypeError):
        return None

    parts = []
    if platform == "twitter":
        if m.get("likes", 0) > 0: parts.append(f"点赞 {m['likes']}")
        if m.get("retweets", 0) > 0: parts.append(f"转发 {m['retweets']}")
        if m.get("views", 0) > 0: parts.append(f"浏览 {m['views']}")
        if m.get("bookmarks", 0) > 0: parts.append(f"收藏 {m['bookmarks']}")
    elif platform == "bilibili":
        if m.get("likes", 0) > 0: parts.append(f"点赞 {m['likes']}")
        if m.get("coins", 0) > 0: parts.append(f"投币 {m['coins']}")
        if m.get("favorites", 0) > 0: parts.append(f"收藏 {m['favorites']}")
        if m.get("comments", 0) > 0: parts.append(f"评论 {m['comments']}")
    elif platform == "reddit":
        if m.get("score", 0) > 0: parts.append(f"score {m['score']}")
        if m.get("num_comments", 0) > 0: parts.append(f"评论 {m['num_comments']}")
    elif platform == "xiaohongshu":
        if m.get("likes", 0) > 0: parts.append(f"点赞 {m['likes']}")
        if m.get("collects", 0) > 0: parts.append(f"收藏 {m['collects']}")
        if m.get("comments", 0) > 0: parts.append(f"评论 {m['comments']}")
    else:
        for k, v in m.items():
            if isinstance(v, (int, float)) and v > 0:
                parts.append(f"{k} {v}")
    return " / ".join(parts) if parts else None


def extract_github_urls(text):
    """Extract GitHub repo URLs from text."""
    pattern = r'https?://github\.com/[\w\-\.]+/[\w\-\.]+'
    return list(set(re.findall(pattern, text or "")))


def build_user_message(item):
    """Build user message from item metadata + title + content/summary."""
    parts = []

    platform = item.get("platform") or ""
    source = item.get("source") or ""
    author = item.get("author_name") or ""

    meta_parts = []
    if platform: meta_parts.append(f"平台: {platform}")
    if source: meta_parts.append(f"来源: {source}")
    if author: meta_parts.append(f"作者: {author}")

    metrics_str = format_metrics(item.get("metrics_json"), platform)
    if metrics_str:
        meta_parts.append(f"互动: {metrics_str}")

    if meta_parts:
        parts.append(" | ".join(meta_parts))

    url = item.get("url") or ""
    content = item.get("content") or ""
    ai_summary = item.get("ai_summary") or ""
    text = ai_summary or content

    github_urls = extract_github_urls(text) or extract_github_urls(url)
    if github_urls:
        parts.append(f"关联仓库: {', '.join(github_urls[:3])}")

    title = item.get("title") or ""
    if title:
        parts.append(f"标题: {title}")

    if text:
        parts.append(f"内容: {text}")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="AI scoring and classification for info2action items (v12.0)")
    parser.add_argument("--limit", type=int, default=100, help="Max items to process (default: 100)")
    parser.add_argument("--force", type=int, default=0, help="Re-score N most recent items (overwrite existing scores)")
    parser.add_argument("--dry-run", action="store_true", help="Print scores without writing to DB")
    parser.add_argument("--since", type=int, default=0, help="Only process items fetched in the last N days")
    parser.add_argument("--ids", type=str, default="", help="Comma-separated item IDs to process (overrides query)")
    args = parser.parse_args()

    config = load_config()
    clf = load_classification()
    ai_config = config.get("ai_summary", {})
    provider = ai_config.get("provider", "minimax")
    api_key = ai_config.get("api_key", "")
    api_base = ai_config.get("api_base", "https://api.minimaxi.com/anthropic/v1")
    model = ai_config.get("model", "MiniMax-M3")
    print(f"Provider: {provider}, Model: {model}", flush=True)
    if provider == "minimax":
        try:
            ai_provider_guard.ensure_provider_available("minimax", source="score_items.main")
        except ai_provider_guard.ProviderCooldown as e:
            print(f"MiniMax cooldown active, skipping scoring until {e.cooldown_until}")
            print(ai_provider_guard.cooldown_message("minimax"))
            return

    if not api_key:
        print("ERROR: No API key in config.json ai_summary section")
        sys.exit(1)

    categories = clf.get("categories", [])
    valid_ids = [cat["id"] for cat in categories]

    conn = db.get_conn()
    ensure_ai_columns(conn)

    # Load community feedback (not user-specific)
    feedback_rows = load_recent_feedback(conn, limit=50)
    feedback_signal = build_feedback_signal(feedback_rows)
    if feedback_rows:
        print(f"Loaded {len(feedback_rows)} feedback entries for community signal")

    # Build system prompt (no user profile)
    system_prompt = build_system_prompt(categories, feedback_signal)

    # Query items to process
    if args.ids:
        id_list = [x.strip() for x in args.ids.split(',') if x.strip()]
        placeholders = ','.join('?' * len(id_list))
        rows = conn.execute(
            f"SELECT id, platform, source, author_name, metrics_json, url, title, content, ai_summary FROM items WHERE id IN ({placeholders})",
            id_list
        ).fetchall()
    elif args.force > 0:
        # bilibili 跳过：B 站视频不走 AI 分类（BF-0418-9）
        rows = conn.execute(
            "SELECT id, platform, source, author_name, metrics_json, url, title, content, ai_summary FROM items WHERE platform != 'bilibili' ORDER BY fetched_at DESC LIMIT ?",
            (args.force,)
        ).fetchall()
    else:
        since_clause = f"AND fetched_at > datetime('now', '-{args.since} days')" if args.since > 0 else ""
        rows = conn.execute(
            f"""SELECT id, platform, source, author_name, metrics_json, url, title, content, ai_summary
                FROM items
                WHERE ai_quality_score IS NULL
                  AND platform != 'bilibili'
                  AND {AI_RETRY_READY_SQL}
                  {since_clause}
                ORDER BY fetched_at DESC LIMIT ?""",
            (args.limit,)
        ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    total = len(items)
    mode_label = "force re-score" if args.force > 0 else "new items"
    print(f"Found {total} items to process ({mode_label})")

    if not items:
        print("All items already scored. Done.")
        return

    max_concurrency = int(ai_config.get("max_concurrency", 5))
    request_interval = float(ai_config.get("request_interval", 0.8))
    completed = 0
    errors = 0
    lock = threading.Lock()

    def process_item(idx, item):
        nonlocal completed, errors
        time.sleep(request_interval)
        user_msg = build_user_message(item)
        if len(user_msg.strip()) < 15:
            record_score_failure(item["id"], "content_too_short", retry_after=24 * 3600, increment=False)
            with lock:
                completed += 1
                errors += 1
            return

        max_retries = 5
        for attempt in range(max_retries):
            try:
                raw = call_llm_score(provider, api_key, api_base, model, system_prompt, user_msg)
                result = parse_score_response(raw, valid_ids)
                break
            except ai_provider_guard.ProviderCooldown as e:
                with lock:
                    completed += 1
                    errors += 1
                    print(f"  [{completed}/{total}] COOLDOWN {item['id'][:20]}: until {e.cooldown_until}", flush=True)
                return
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    record_score_failure(item["id"], f"HTTP {e.code}", retry_after=30 * 60)
                with lock:
                    completed += 1
                    errors += 1
                    print(f"  [{completed}/{total}] ERROR {item['id'][:20]}: {str(e)[:60]}", flush=True)
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(3 + random.uniform(1, 3))
                    continue
                record_score_failure(item["id"], str(e), retry_after=30 * 60)
                with lock:
                    completed += 1
                    errors += 1
                    print(f"  [{completed}/{total}] ERROR {item['id'][:20]}: {str(e)[:60]}", flush=True)
                return

        if result is None:
            record_score_failure(item["id"], "score_parse_error", retry_after=30 * 60)
            with lock:
                completed += 1
                errors += 1
                print(f"  [{completed}/{total}] PARSE_ERR {item['id'][:20]}", flush=True)
            return

        category, content_type, quality_score, legacy_score, reason, keywords, dimensions = result
        title_short = (item.get("title") or "")[:50]

        if not args.dry_run:
            try:
                item_conn = db.get_conn()
                kw_json = json.dumps(keywords, ensure_ascii=False) if keywords else None
                dim_json = json.dumps(dimensions, ensure_ascii=False) if dimensions else None
                item_conn.execute(
                    """UPDATE items SET ai_category=?, relevance_score=?, ai_keywords=?,
                       ai_dimensions=?, content_type=?, ai_quality_score=?,
                       ai_error_count=0, ai_last_error=NULL,
                       ai_last_error_at=NULL, ai_retry_after=NULL
                       WHERE id=?""",
                    (category, legacy_score, kw_json, dim_json, content_type, quality_score, item["id"])
                )
                item_conn.commit()
                item_conn.close()
            except Exception as e:
                record_score_failure(item["id"], f"db_error: {e}", retry_after=30 * 60)
                with lock:
                    completed += 1
                    errors += 1
                    print(f"  [{completed}/{total}] DB_ERR {item['id'][:20]}: {e}", flush=True)
                return

        with lock:
            completed += 1
            prefix = "[DRY] " if args.dry_run else ""
            dim_str = " ".join(f"{k}={v}" for k, v in sorted(dimensions.items()))
            print(f'  {prefix}[{completed}/{total}] {category}/{content_type} q={quality_score:.3f} {dim_str} "{title_short}"', flush=True)

    print(f"Starting scoring (concurrency: {max_concurrency}, dry_run: {args.dry_run})...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [executor.submit(process_item, i, item) for i, item in enumerate(items)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  [THREAD_ERR] {e}", flush=True)

    elapsed = time.time() - start_time
    print(f"\nDone! {completed} items in {elapsed:.1f}s ({completed/max(elapsed,0.1):.1f}/s)")
    print(f"Errors: {errors}")
    if args.dry_run:
        print("(dry-run mode — no changes written to DB)")


if __name__ == "__main__":
    main()
