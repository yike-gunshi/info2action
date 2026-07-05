#!/usr/bin/env python3
"""
AI Summary Generator for info2action
Uses MiniMax Token Plan API (Anthropic-compatible) with up to 20 concurrency.
Reads items from SQLite DB, generates summaries, writes back to DB.
"""

import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.request
import urllib.error
import ssl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
AI_RETRY_READY_SQL = "(ai_retry_after IS NULL OR ai_retry_after <= datetime('now'))"

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def call_openai(api_key, api_base, model, prompt, content, max_tokens=4096):
    """Call OpenAI-compatible API (ChatCompletions format)."""
    url = f"{api_base}/chat/completions"
    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    })

    ctx = ssl.create_default_context()
    backoff = [5, 15, 30, 60, 120]
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choices = result.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
                return "[总结生成失败: no choices in response]"
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                wait = backoff[attempt]
                time.sleep(wait)
                req = urllib.request.Request(url, data=payload, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"})
                continue
            return f"[总结生成失败: HTTP {e.code}]"
        except Exception as e:
            return f"[总结生成失败: {str(e)[:80]}]"
    return "[总结生成失败: max retries]"


def call_minimax(api_key, api_base, model, prompt, content, max_tokens=4096):
    """Call MiniMax Token Plan API (Anthropic-compatible format)."""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": prompt,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": content}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    })

    ctx = ssl.create_default_context()
    try:
        with ai_provider_guard.guarded_urlopen(
            req,
            source="generate_summaries",
            timeout=60,
            context=ctx,
        ) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            for block in result["content"]:
                if block.get("type") == "text":
                    return block["text"].strip()
            return "[总结生成失败: no text block in response]"
    except ai_provider_guard.ProviderCooldown as e:
        return f"[总结生成失败: minimax cooldown until {e.cooldown_until}]"
    except urllib.error.HTTPError as e:
        return f"[总结生成失败: HTTP {e.code}]"
    except Exception as e:
        return f"[总结生成失败: {str(e)[:50]}]"


def call_llm(provider, api_key, api_base, model, prompt, content, max_tokens=4096):
    """Dispatch to the correct LLM provider."""
    if provider == 'openai':
        return call_openai(api_key, api_base, model, prompt, content, max_tokens)
    else:
        return call_minimax(api_key, api_base, model, prompt, content, max_tokens)

def build_prompt(source, category="", has_asr=False):
    """Build summary prompt based on content source and category.

    v13.0: has_asr=True 时优先用 `02_summary_breakdown_asr.md`(适配长 ASR 转写)。
    has_asr 由 caller 根据 item.asr_text 非空来决定。
    """
    from prompt_loader import load_prompt
    if has_asr:
        prompt = load_prompt('02_summary_breakdown_asr.md', category=category or "未分类")
        if prompt:
            return prompt
        # fallthrough 到通用 prompt
    prompt = load_prompt('02_summary_breakdown.md', category=category or "未分类")
    if prompt:
        return prompt
    # Fallback if file missing
    return """你是信息精选助手。请为以下内容生成两部分摘要：

【精华速览】
一段连贯的摘要，让读者不点开原文就能掌握核心信息。
规范：覆盖核心事实、关键数据/结论，以及影响或意义（如有）。长度自适应，不凑字也不压缩。
对关键实体（产品名、公司名、数字、百分比、版本号）使用 **加粗** 标记。
直接陈述事实，不以"本文介绍了"开头，不加"值得关注"等主观评价。

【全文拆解】
按内容的主要话题分组，与精华速览互补——速览给全局结论，拆解给可扫读的结构化细节。
每组格式：
**话题标题**（5-15字，只写核心主题）
- 子要点（一句话，保留关键数据和实体名称）
- 子要点

规范：话题数量由内容复杂度决定，不人为限制。每个话题必须有实质信息增量，不为分组而分组。
子要点中的关键实体同样使用 **加粗** 标记。
【精华速览】和【全文拆解】两个标记必须始终输出，不可省略。
直接输出，不要有多余的前缀或解释。"""


def parse_summary_response(text):
    """Parse the two-section response into structured data."""
    import re
    preview = ""
    key_points = []

    sections = text.split("【全文拆解】")
    if len(sections) == 2:
        preview_part = sections[0].replace("【精华速览】", "").strip()
        preview = preview_part

        breakdown_part = sections[1].strip()
        current_section = None
        for line in breakdown_part.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Match numbered section header: "1. 话题标题" or "1、话题标题" or "**1. 话题标题**"
            clean = line.strip("*").strip()
            m = re.match(r'^\d+[.、]\s*(.+)$', clean)
            if m:
                if current_section:
                    key_points.append(current_section)
                title_text = m.group(1).strip().strip("*").strip()
                current_section = {"title": title_text, "points": []}
            elif line.startswith(("- ", "• ", "* ")):
                point = line.lstrip("-•* ").strip()
                if point:
                    if current_section is not None:
                        current_section["points"].append(point)
                    else:
                        # Model output flat bullets without numbered headers —
                        # treat each as a standalone topic
                        key_points.append({"title": point, "points": []})
        if current_section:
            key_points.append(current_section)
    else:
        # Fallback: try old 【关键信息】 split
        sections2 = text.split("【关键信息】")
        if len(sections2) == 2:
            preview = sections2[0].replace("【精华速览】", "").strip()
            for line in sections2[1].split("\n"):
                line = line.strip().lstrip("- ").lstrip("• ").strip()
                if line:
                    key_points.append(line)
        else:
            preview = text.strip()

    return {"preview": preview, "key_points": key_points}

def build_content_text(item, source):
    """Extract all available text content from an item for summarization."""
    parts = []

    # Title
    title = item.get("title") or item.get("display_title") or ""
    if title:
        parts.append(f"标题: {title}")

    # Author
    author = item.get("author") or item.get("user", {}).get("nickname") or ""
    if author:
        parts.append(f"作者: {author}")

    # Main text/body
    text = item.get("text") or item.get("desc") or item.get("body") or ""
    if text:
        parts.append(f"正文: {text}")

    # Comments (XHS)
    comments = item.get("comments") or []
    if comments:
        comment_texts = []
        for c in comments[:5]:
            ct = c.get("content") or c.get("text") or ""
            if ct:
                comment_texts.append(ct)
        if comment_texts:
            parts.append(f"热门评论: {'; '.join(comment_texts)}")

    # Subtitle/ASR (Bilibili)
    subtitle = item.get("subtitle") or ""
    if subtitle:
        parts.append(f"字幕: {subtitle[:2000]}")

    ai_summary = item.get("ai_summary") or ""
    if ai_summary:
        parts.append(f"AI总结(平台): {ai_summary}")

    return "\n".join(parts)


def query_pending_items(conn, limit=None, specific_ids=None, rerun_days=None):
    select_cols = "id, platform, title, content, ai_summary, ai_category as category, detail_json, asr_text"
    if specific_ids:
        placeholders = ','.join('?' * len(specific_ids))
        return conn.execute(
            f"SELECT {select_cols} FROM items WHERE id IN ({placeholders})",
            specific_ids
        ).fetchall()

    if rerun_days:
        return conn.execute(
            f"""SELECT {select_cols} FROM items
               WHERE fetched_at >= datetime('now', ?)
               AND platform != 'bilibili'
               AND {AI_RETRY_READY_SQL}
               AND (ai_key_points IS NULL OR ai_key_points = '' OR ai_key_points NOT LIKE '%"title"%')""",
            (f'-{rerun_days} days',)
        ).fetchall()

    limit_clause = " LIMIT ?" if limit else ""
    params = (limit,) if limit else ()
    return conn.execute(
        f"""SELECT {select_cols} FROM items
           WHERE platform != 'bilibili'
           AND {AI_RETRY_READY_SQL}
           AND (ai_summary IS NULL OR ai_summary = '')
           ORDER BY fetched_at DESC{limit_clause}""",
        params
    ).fetchall()


def record_summary_failure(item_id, error, retry_after=None, increment=True):
    item_conn = db.get_conn()
    try:
        db.record_ai_failure(item_conn, item_id, error, retry_after=retry_after, increment=increment)
    finally:
        item_conn.close()

def main():
    config = load_config()
    ai_config = config.get("ai_summary", {})
    provider = ai_config.get("provider", "minimax")
    # oss-release F3c: env/.env 优先（对齐 resolve_minimax_*_config），config.json 只留空模板
    from env_utils import load_project_env
    project_env = load_project_env(BASE_DIR)
    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or project_env.get("MINIMAX_API_KEY")
        or ai_config.get("api_key", "")
        or ""
    ).strip()
    api_base = (
        os.environ.get("MINIMAX_API_BASE")
        or project_env.get("MINIMAX_API_BASE")
        or ai_config.get("api_base", "")
        or "https://api.minimaxi.com/anthropic/v1"
    ).strip()
    model = ai_config.get("model", "MiniMax-M2.7")
    print(f"Provider: {provider}, Model: {model}", flush=True)
    if provider == "minimax":
        try:
            ai_provider_guard.ensure_provider_available("minimax", source="generate_summaries.main")
        except ai_provider_guard.ProviderCooldown as e:
            print(f"MiniMax cooldown active, skipping summaries until {e.cooldown_until}")
            print(ai_provider_guard.cooldown_message("minimax"))
            return
    # Parse args: [concurrency] or --ids <id1,id2,...> or --days <N> or --limit <N>
    specific_ids = None
    rerun_days = None
    limit = None
    max_concurrency = ai_config.get("max_concurrency", 20)
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--ids' and i + 1 < len(args):
            specific_ids = args[i + 1].split(',')
            i += 2
            continue
        elif arg == '--days' and i + 1 < len(args):
            rerun_days = int(args[i + 1])
            i += 2
            continue
        elif arg == '--limit' and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
            continue
        elif arg.isdigit():
            max_concurrency = int(arg)
        i += 1
    # v13.0: 默认 100000 支撑长 ASR 转写摘要;config.json 同步更新
    max_tokens = ai_config.get("max_tokens", 100000)

    if not api_key:
        print("ERROR: No API key in config.json ai_summary section")
        sys.exit(1)

    # Query items from DB
    conn = db.get_conn()
    rows = query_pending_items(
        conn,
        limit=limit,
        specific_ids=specific_ids,
        rerun_days=rerun_days,
    )
    items_from_db = [dict(r) for r in rows]
    conn.close()

    print(f"Found {len(items_from_db)} items without summaries in DB", flush=True)

    if not items_from_db:
        print("All items already have summaries. Done.", flush=True)
        return

    # Build content text for each item, enriching with referenced_urls full_text
    to_summarize = []
    for item in items_from_db:
        title = item.get('title') or ''
        content = item.get('content') or ''
        asr_text = item.get('asr_text') or ''  # v13.0: 视频 ASR 转写,存在时优先作主输入

        # Extract full_text from enriched referenced_urls in detail_json
        enriched_text = ''
        dj_raw = item.get('detail_json')
        if dj_raw:
            try:
                import json as _json
                dj = _json.loads(dj_raw) if isinstance(dj_raw, str) else dj_raw
                ref_urls = dj.get('referenced_urls', [])
                for ref in ref_urls:
                    ft = ref.get('full_text', '')
                    if ft and len(ft) > 100:
                        ref_title = ref.get('title', '')
                        enriched_text += f"\n\n--- 外链正文: {ref_title} ---\n{ft}"
                        break  # Use the first substantial full_text
            except (ValueError, TypeError, AttributeError):
                pass

        # v13.0: asr_text 非空 → 以 ASR 转写为主输入,把 title/content 作为上下文注入
        has_asr = bool(asr_text and asr_text.strip())
        if has_asr:
            content_text = (
                f"视频标题: {title or '(无)'}\n"
                f"视频简介/正文: {content or '(无)'}\n\n"
                f"以下是该视频的 AI 语音转写(ASR transcript):\n\n"
                f"{asr_text}"
            )
            # ASR 场景不截断到 12k,让 max_tokens 去容纳;但设一个安全上限
            content_text = content_text[:200000]
        else:
            # Assemble: title + content + enriched full_text
            content_text = f"标题: {title}\n正文: {content or ''}"
            if enriched_text:
                content_text += enriched_text
            # Cap total length
            content_text = content_text[:12000]

        # Skip items with too little content (title-only, no real body)
        body_text = asr_text + (content or '') + enriched_text
        if len(body_text.strip()) < 15:
            record_summary_failure(item["id"], "content_too_short", retry_after=24 * 3600, increment=False)
            continue
        to_summarize.append({
            "id": item["id"],
            "platform": item["platform"],
            "content_text": content_text,
            "title": title[:100],
            "category": item.get("category") or "",
            "has_asr": has_asr,
        })

    print(f"Need to summarize: {len(to_summarize)} items", flush=True)

    if not to_summarize:
        print("No items with sufficient content to summarize. Done.")
        return

    # Generate summaries with concurrency
    completed = 0
    errors = 0
    lock = threading.Lock()

    def process_item(item):
        nonlocal completed, errors
        is_error = False
        try:
            time.sleep(0.5)  # Rate limit: stagger requests
            prompt = build_prompt(item.get("platform", ""), item.get("category", ""),
                                  has_asr=bool(item.get("has_asr")))
            raw = call_llm(provider, api_key, api_base, model, prompt, item["content_text"], max_tokens)
            parsed = parse_summary_response(raw)
            summary = parsed["preview"]
            key_points = parsed.get("key_points", [])

            is_error = summary.startswith("[总结生成失败")
            if is_error:
                print(f"  [FAIL] {item['id'][:25]}: {summary[:80]}", flush=True)
                if "minimax cooldown" not in summary:
                    record_summary_failure(item["id"], summary, retry_after=30 * 60)

            # Validate key_points is structured [{title, points}], skip if not
            if not is_error and (not key_points or not isinstance(key_points[0], dict) or "title" not in key_points[0]):
                print(f"  [SKIP] {item['id'][:25]}: key_points 格式异常，不写入", flush=True)
                is_error = True
                record_summary_failure(item["id"], "key_points_parse_error", retry_after=30 * 60)

            # Write to DB
            if not is_error:
                try:
                    item_conn = db.get_conn()
                    db.update_ai_summary(item_conn, item["id"], summary, key_points)
                    item_conn.close()
                except Exception as e:
                    print(f"  [DB ERROR] {item['id']}: {e}", flush=True)
        except Exception as e:
            print(f"  [ITEM ERROR] {item.get('id', '?')}: {e}", flush=True)
            is_error = True
            record_summary_failure(item.get("id", ""), str(e), retry_after=30 * 60)

        with lock:
            completed += 1
            if is_error:
                errors += 1
            if completed % 10 == 0 or completed == len(to_summarize):
                print(f"  Progress: {completed}/{len(to_summarize)} (errors: {errors})", flush=True)

    print(f"Starting summary generation (concurrency: {max_concurrency})...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [executor.submit(process_item, item) for item in to_summarize]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"  [ERROR] {e}")

    elapsed = time.time() - start_time

    print(f"\nDone! {completed} summaries in {elapsed:.1f}s ({completed/max(elapsed,0.1):.1f}/s)")
    print(f"Errors: {errors}")
    print(f"Saved to DB.")

if __name__ == "__main__":
    main()
