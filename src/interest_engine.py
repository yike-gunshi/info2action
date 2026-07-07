#!/usr/bin/env python3
"""
v6.0: 兴趣语义检索引擎
批量调 MiniMax API 对 items 做语义匹配，结果写入 interest_matches 表。
可作为独立脚本运行，也可被 serve.py import 调用。
"""

import json
import os
import sys
import time
import ssl
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db
import remote_db
from env_utils import load_project_env

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
BATCH_SIZE = 20  # 每批 item 数
MAX_WORKERS = 5  # 最大并发数
_DEFAULT_MINIMAX_CHAT_BASE = "https://api.minimaxi.com/anthropic/v1"
_DEFAULT_MINIMAX_CHAT_MODEL = "MiniMax-M3"


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def resolve_minimax_runtime_config(ai_config):
    """Resolve MiniMax chat runtime config for interest scanning."""
    ai_config = ai_config or {}
    project_env = load_project_env(BASE_DIR)
    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or project_env.get("MINIMAX_API_KEY")
        or ai_config.get("api_key")
        or ""
    ).strip()
    api_base = (
        os.environ.get("MINIMAX_API_BASE")
        or project_env.get("MINIMAX_API_BASE")
        or ai_config.get("api_base")
        or _DEFAULT_MINIMAX_CHAT_BASE
    ).strip().rstrip("/")
    model = (
        os.environ.get("MINIMAX_MODEL")
        or project_env.get("MINIMAX_MODEL")
        or ai_config.get("model")
        or _DEFAULT_MINIMAX_CHAT_MODEL
    ).strip()
    return api_key, api_base, model


def call_minimax(api_key, api_base, model, system_prompt, user_content, max_tokens=2048):
    """调用 MiniMax Token Plan API（Anthropic 兼容格式）。"""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": user_content[:12000]}
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
            source="interest_engine",
            timeout=45,
            context=ctx,
        ) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            for block in result["content"]:
                if block.get("type") == "text":
                    return block["text"].strip()
            return None
    except ai_provider_guard.ProviderCooldown as e:
        print(f"  MiniMax cooldown active, skipping interest call until {e.cooldown_until}")
        return None
    except urllib.error.HTTPError as e:
        print(f"  HTTP error: {e.code}")
        return None
    except Exception as e:
        print(f"  API error: {str(e)[:80]}")
        return None


def build_scan_prompt(keywords):
    """构建语义匹配的 system prompt。"""
    kw_str = ', '.join(keywords)

    from prompt_loader import load_prompt
    prompt = load_prompt('03_interest_matching.md', keywords=kw_str)
    if prompt:
        return prompt

    return f"""你是信息相关性判断助手。用户关注以下方向：

关键词：{kw_str}

你需要判断每条信息与用户关注方向的相关程度。

评分标准（0-10）：
- 9-10: 直接相关，核心话题，高价值
- 7-8: 较相关，有实质性关联
- 5-6: 间接相关，可能有参考价值
- 3-4: 弱相关，仅边缘涉及
- 0-2: 不相关

对每条信息，只需返回 ID 和分数。

输出格式：严格 JSON 数组，不要其他内容（不要 markdown 代码块标记）。
只返回分数 >= 5 的条目。如果没有相关条目，返回空数组 []。

[{{"id": "item_id_1", "score": 8}}, {{"id": "item_id_2", "score": 6}}]"""


def build_batch_content(items):
    """构建一批 item 的用户内容。"""
    lines = []
    for it in items:
        title = (it.get('title') or '')[:100]
        summary = (it.get('ai_summary') or '')[:200]
        key_points = ''
        kp_raw = it.get('ai_key_points')
        if kp_raw:
            if isinstance(kp_raw, str):
                try:
                    kp_raw = json.loads(kp_raw)
                except Exception:
                    kp_raw = []
            if isinstance(kp_raw, list):
                key_points = '; '.join(kp_raw[:3])
        lines.append(f"[{it['id']}] {title}")
        if summary:
            lines.append(f"  摘要: {summary}")
        if key_points:
            lines.append(f"  要点: {key_points}")
        lines.append('')
    return '\n'.join(lines)


def parse_scan_response(text):
    """解析 AI 返回的匹配结果。"""
    if not text:
        return []
    text = text.strip()
    # 去除 markdown 代码块标记
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            item_id = item.get('id', '')
            score = item.get('score', 0)
            if item_id and isinstance(score, (int, float)) and score >= 5:
                results.append({'item_id': str(item_id), 'relevance_score': float(score)})
        return results
    except json.JSONDecodeError:
        # 兜底：尝试正则提取
        import re
        results = []
        for m in re.finditer(r'"id"\s*:\s*"([^"]+)"[^}]*"score"\s*:\s*(\d+(?:\.\d+)?)', text):
            item_id = m.group(1)
            score = float(m.group(2))
            if score >= 5:
                results.append({'item_id': item_id, 'relevance_score': score})
        return results


def fetch_items_for_scan(conn, scope='all', since=None):
    """获取待扫描的 items。scope: all / 7d / 30d。since: 增量扫描起始时间。"""
    where = []
    params = []

    # 只扫描有 AI 摘要的内容
    where.append("ai_summary IS NOT NULL AND ai_summary != ''")

    if since:
        where.append("fetched_at > ?")
        params.append(since)
    elif scope == '7d':
        where.append("fetched_at > datetime('now', '-7 days')")
    elif scope == '30d':
        where.append("fetched_at > datetime('now', '-30 days')")
    # scope == 'all' 不加时间过滤

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(f"""
        SELECT id, title, ai_summary, ai_key_points
        FROM items {where_sql}
        ORDER BY fetched_at DESC
    """, params).fetchall()
    return [dict(r) for r in rows]


def _backfill_suggestion(conn, interest_id, interest, ai_config):
    """补生成行动建议（用已有 matches 的 top items）。"""
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)
    if not api_key:
        return
    matches = db.get_interest_matches(conn, interest_id, limit=10)
    if not matches:
        return
    # 拿 top match 的 item 信息
    item_ids = [m['item_id'] for m in matches[:5]]
    items = []
    for iid in item_ids:
        row = conn.execute("SELECT id, title, ai_summary FROM items WHERE id=?", (iid,)).fetchone()
        if row:
            items.append(dict(row))
    if not items:
        return
    fake_matches = [{'item_id': it['id'], 'relevance_score': 10} for it in items]
    sug = generate_interest_suggestion(api_key, api_base, model, interest, fake_matches, items)
    if sug:
        db.update_interest(conn, interest_id,
                           suggestion=json.dumps(sug, ensure_ascii=False))
        print(f"  兴趣 #{interest_id} 补生成行动建议: {sug.get('title', '')}")


def _backfill_suggestion_remote(interest_id, interest, ai_config):
    """Remote-db variant of _backfill_suggestion."""
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)
    if not api_key:
        return
    items = remote_db.get_interest_top_items_remote(interest_id, limit=5)
    if not items:
        return
    fake_matches = [{'item_id': it['id'], 'relevance_score': 10} for it in items]
    sug = generate_interest_suggestion(api_key, api_base, model, interest, fake_matches, items)
    if sug:
        remote_db.update_interest_remote(interest_id, suggestion=sug)
        print(f"  兴趣 #{interest_id} 补生成行动建议: {sug.get('title', '')}")


def scan_interest(interest_id, progress_callback=None):
    """
    对指定兴趣配置执行全量/增量语义扫描。
    progress_callback: 可选回调函数 (processed, total) 用于进度通知。
    返回匹配结果数量。
    """
    remote = remote_db.app_state_to_remote()
    conn = None
    if remote:
        interest = remote_db.get_interest_remote(interest_id)
    else:
        conn = db.get_conn()
        interest = db.get_interest(conn, interest_id)
    if not interest:
        if conn:
            conn.close()
        return 0

    keywords = interest.get('keywords', [])
    if not keywords:
        if conn:
            conn.close()
        return 0

    # 更新扫描状态
    if remote:
        remote_db.update_interest_remote(interest_id, scan_status='scanning')
    else:
        db.update_interest(conn, interest_id, scan_status='scanning')

    # 加载 AI 配置（提前加载，backfill 也需要）
    config = load_config()
    ai_config = config.get("ai_summary", {})

    scope = interest.get('scope', 'all')
    last_scan = interest.get('last_scan_at')

    # 增量：如果有上次扫描时间，只扫描新 items
    since = last_scan if last_scan else None
    if remote:
        items = remote_db.fetch_items_for_interest_scan_remote(scope=scope, since=since)
    else:
        items = fetch_items_for_scan(conn, scope, since)
        conn.close()

    if not items:
        if remote:
            remote_db.update_interest_remote(
                interest_id,
                scan_status='done',
                last_scan_at=datetime.now().isoformat(),
            )
        else:
            conn = db.get_conn()
            db.update_interest(conn, interest_id,
                               scan_status='done',
                               last_scan_at=datetime.now().isoformat())
        # v6.0.1: 即使无新 items，若 suggestion 为空则补生成
        if not interest.get('suggestion'):
            if remote:
                _backfill_suggestion_remote(interest_id, interest, ai_config)
            else:
                _backfill_suggestion(conn, interest_id, interest, ai_config)
        if not remote:
            conn.close()
        return 0
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)

    if not api_key:
        print("ERROR: 无 MiniMax API key（请配置 .env 或 config.json）")
        if remote:
            remote_db.update_interest_remote(interest_id, scan_status='pending')
        else:
            conn = db.get_conn()
            db.update_interest(conn, interest_id, scan_status='pending')
            conn.close()
        return 0

    system_prompt = build_scan_prompt(keywords)

    # 分批处理
    batches = [items[i:i+BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    total = len(items)
    processed = 0
    all_matches = []

    def process_batch(batch):
        content = build_batch_content(batch)
        text = call_minimax(api_key, api_base, model, system_prompt, content)
        return parse_scan_response(text)

    # 并发执行
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_batch, batch): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            try:
                matches = future.result()
                all_matches.extend(matches)
            except Exception as e:
                print(f"  批次扫描错误: {e}")
            processed += BATCH_SIZE
            if progress_callback:
                progress_callback(min(processed, total), total)

    # 写入匹配结果
    if all_matches:
        if remote:
            remote_db.upsert_interest_matches_remote(interest_id, all_matches)
        else:
            conn = db.get_conn()
            db.upsert_interest_matches(conn, interest_id, all_matches)
            conn.close()

    # 更新扫描状态
    if remote:
        remote_db.update_interest_remote(
            interest_id,
            scan_status='done',
            last_scan_at=datetime.now().isoformat(),
        )
    else:
        conn = db.get_conn()
        db.update_interest(conn, interest_id,
                           scan_status='done',
                           last_scan_at=datetime.now().isoformat())
        conn.close()

    # v6.0.1: 生成行动建议
    if all_matches:
        suggestion = generate_interest_suggestion(
            api_key, api_base, model, interest, all_matches, items)
        if suggestion:
            if remote:
                remote_db.update_interest_remote(interest_id, suggestion=suggestion)
            else:
                conn = db.get_conn()
                db.update_interest(conn, interest_id,
                                   suggestion=json.dumps(suggestion, ensure_ascii=False))
                conn.close()

    print(f"  兴趣 #{interest_id} 扫描完成: {len(all_matches)} 条匹配 / {total} 条扫描")
    return len(all_matches)


def generate_interest_suggestion(api_key, api_base, model, interest, matches, items):
    """基于匹配结果为兴趣方向生成一条行动建议。"""
    # 取 top-3 匹配的 item 信息
    top_ids = {m['item_id'] for m in sorted(matches, key=lambda x: x['relevance_score'], reverse=True)[:3]}
    top_items = [it for it in items if it['id'] in top_ids]
    if not top_items:
        return None

    items_text = '\n'.join(
        f"- {it.get('title', '')[:80]}: {(it.get('ai_summary') or '')[:120]}"
        for it in top_items
    )
    kw_str = ', '.join(interest.get('keywords', []))

    from prompt_loader import load_prompt
    system_prompt = load_prompt('03c_interest_action.md',
                                interest_name=interest.get('name', ''),
                                keywords=kw_str)
    if not system_prompt:
        system_prompt = f"""你是信息行动建议助手。用户关注方向：{interest.get('name', '')}（关键词：{kw_str}）

基于最相关的信息，生成 1 条可执行的行动建议。

输出格式：严格 JSON，不要其他内容（不要 markdown 代码块标记）。
{{"title": "建议标题（动词开头，10字以内）", "reason": "为什么推荐（1句话，不超过50字）"}}"""

    text = call_minimax(api_key, api_base, model, system_prompt,
                        f"最相关的信息：\n{items_text}", max_tokens=200)
    if not text:
        return None

    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        sug = json.loads(text)
        if isinstance(sug, dict) and sug.get('title'):
            return sug
    except json.JSONDecodeError:
        pass
    return None


def scan_all_interests(incremental=True):
    """扫描所有启用的兴趣配置。incremental=True 时仅扫描新 items。"""
    if remote_db.app_state_to_remote():
        interests = remote_db.list_interests_remote()
    else:
        conn = db.get_conn()
        interests = db.list_interests(conn)
        conn.close()

    enabled = [i for i in interests if i.get('enabled', 1)]
    if not enabled:
        print("无启用的兴趣配置，跳过扫描")
        return

    print(f"开始扫描 {len(enabled)} 个兴趣方向...")
    for interest in enabled:
        print(f"  扫描: {interest['name']} (ID={interest['id']})")
        scan_interest(interest['id'])


def generate_keywords(description):
    """从自然语言描述生成检索关键词。"""
    config = load_config()
    ai_config = config.get("ai_summary", {})
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)

    if not api_key:
        return []

    from prompt_loader import load_prompt
    system_prompt = load_prompt('03b_interest_keywords.md')
    if not system_prompt:
        system_prompt = """你是关键词生成助手。根据用户的兴趣描述，生成 5-10 个用于信息检索的关键词。

要求：
1. 提取专有名词（产品名、技术名、公司名）
2. 提取核心主题词
3. 中英文混合（如果涉及英文产品/技术）
4. 避免太宽泛的词（如"AI"、"技术"）

输出格式：严格 JSON 数组，不要其他内容（不要 markdown 代码块标记）。
["关键词1", "关键词2", "关键词3"]"""

    text = call_minimax(api_key, api_base, model, system_prompt, description, max_tokens=200)
    if not text:
        return []

    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        keywords = json.loads(text)
        if isinstance(keywords, list):
            return [str(kw).strip() for kw in keywords if kw and str(kw).strip()]
        return []
    except json.JSONDecodeError:
        return []


if __name__ == "__main__":
    print("=" * 50)
    print("  兴趣语义检索引擎")
    print("=" * 50)

    try:
        ai_provider_guard.ensure_provider_available("minimax", source="interest_engine.main")
    except ai_provider_guard.ProviderCooldown as e:
        print(f"MiniMax cooldown active, skipping interest scan until {e.cooldown_until}")
        print(ai_provider_guard.cooldown_message("minimax"))
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == '--interest':
        # 扫描指定兴趣
        interest_id = int(sys.argv[2])
        scan_interest(interest_id,
                      progress_callback=lambda p, t: print(f"  进度: {p}/{t}"))
    else:
        # 扫描所有启用的兴趣
        scan_all_interests(incremental=True)

    print("完成!")
