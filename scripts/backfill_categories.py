#!/usr/bin/env python3
"""
One-shot backfill: classify existing items using AI.
Reads classification.json as the pluggable framework, sends title+summary to MiniMax,
writes ai_category back to DB.

Usage: python scripts/backfill_categories.py [concurrency]
"""
import json, os, sys, time, re, ssl, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request

# SSL context for environments with self-signed certs in chain
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import db

def load_config():
    with open(os.path.join(BASE_DIR, "config", "config.json")) as f:
        return json.load(f)

def load_classification():
    with open(os.path.join(BASE_DIR, "config", "classification.json")) as f:
        return json.load(f)

def build_classify_prompt(clf):
    """Build a compact classification-only prompt from classification.json."""
    cats = clf["categories"]
    valid_ids = []
    cat_lines = []
    for cat in cats:
        cid = cat["id"]
        valid_ids.append(cid)
        name = cat["name"]
        desc = cat["description"]
        examples = ", ".join(cat.get("examples_in", [])[:3])
        cat_lines.append(f"- {cid}（{name}）：{desc}")
        if examples:
            cat_lines.append(f"  典型：{examples}")

    cat_block = "\n".join(cat_lines)

    from prompt_loader import load_prompt
    prompt = load_prompt('01b_classify_backfill.md', categories=cat_block)
    if prompt:
        return prompt, valid_ids

    # Fallback if file missing
    lines = ["你是内容分类助手。请将以下内容归入最合适的分类。只输出分类ID，不要输出其他任何内容。\n"]
    lines.append(cat_block)
    lines.append("")
    lines.append("分类判断优先级：先看主题主体（官方产品/功能→products，提效工具/开发者能力→ai_tools，模型本身→models，系统设计/技术机制→tech），再判断是否属于教程/行业/创作/投资。")
    lines.append("提到模型名（如Claude/GPT）不等于属于 models 分类。")
    lines.append("\n只输出一个分类ID（如 products/ai_tools/models/tech/tutorials/industry/creator/investment/other），不要有其他文字。")
    return "\n".join(lines), valid_ids

def call_api(api_key, api_base, model, prompt, content, max_tokens=20):
    url = f"{api_base}/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content[:4000]}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    })
    with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"].strip()

def parse_category(raw, valid_ids):
    """Extract category ID from AI response."""
    raw_lower = raw.lower().strip()
    for cid in valid_ids:
        if cid in raw_lower:
            return cid
    return None

def main():
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    config = load_config()
    clf = load_classification()
    ai = config.get("ai_summary", {})
    api_key = ai.get("api_key", "")
    api_base = ai.get("api_base", "https://api.minimaxi.com/v1")
    model = ai.get("model", "MiniMax-Text-01")

    if not api_key:
        print("ERROR: No API key"); sys.exit(1)

    prompt, valid_ids = build_classify_prompt(clf)

    # Get items without category
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, title, ai_summary FROM items WHERE ai_category IS NULL OR ai_category = ''"
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    print(f"Found {len(items)} items without category")
    if not items:
        print("All items already classified."); return

    completed = 0
    errors = 0
    lock = threading.Lock()

    def classify(item):
        nonlocal completed, errors
        text = f"标题: {item.get('title') or ''}\nAI摘要: {item.get('ai_summary') or ''}"
        if len(text.strip()) < 15:
            text = f"标题: {item.get('title') or '(无标题)'}"
        try:
            raw = call_api(api_key, api_base, model, prompt, text)
            cat = parse_category(raw, valid_ids)
            if cat:
                item_conn = db.get_conn()
                db.update_ai_category(item_conn, item["id"], cat)
                item_conn.close()
        except Exception as e:
            with lock:
                errors += 1
            return

        with lock:
            completed += 1
            if completed % 20 == 0 or completed == len(items):
                print(f"  Progress: {completed}/{len(items)} (errors: {errors})")

    print(f"Classifying {len(items)} items (concurrency: {concurrency})...")
    start = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(classify, it) for it in items]
        for f in as_completed(futures):
            try: f.result()
            except: pass

    elapsed = time.time() - start
    print(f"\nDone! {completed} classified in {elapsed:.1f}s, {errors} errors")

if __name__ == "__main__":
    main()
