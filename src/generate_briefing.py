#!/usr/bin/env python3
"""
v5.0: AI Briefing Generator for info2action
Reads high-score items from last 24h, user context, and generates:
  - 3-8 aggregated insights (multi-source → one insight)
  - 2-5 action suggestions
Uses MiniMax Token Plan API (Anthropic-compatible).
"""

import json
import os
import sys
import time

import urllib.request
import urllib.error
import ssl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db
import remote_db
from env_utils import load_project_env

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
USER_CONTEXT_PATH = os.path.join(BASE_DIR, "config", "user_context.json")
_DEFAULT_MINIMAX_CHAT_BASE = "https://api.minimaxi.com/anthropic/v1"
_DEFAULT_MINIMAX_CHAT_MODEL = "MiniMax-M3"


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_user_context():
    """Load user context, return defaults if file missing."""
    defaults = {
        "role": "AI 产品创业者",
        "goals": ["发现创业方向", "跟踪 AI 工具", "学习技术趋势"],
        "projects": ["info2action 信息雷达"],
        "interests": "AI 产品、创业、LLM 应用"
    }
    try:
        with open(USER_CONTEXT_PATH, "r") as f:
            ctx = json.load(f)
        # Merge with defaults for missing keys
        for k, v in defaults.items():
            if k not in ctx:
                ctx[k] = v
        return ctx
    except Exception:
        return defaults


def resolve_minimax_runtime_config(ai_config):
    """Resolve MiniMax chat runtime config for briefing generation."""
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


def call_minimax(api_key, api_base, model, system_prompt, user_content, max_tokens=4096):
    """Call MiniMax Token Plan API (Anthropic-compatible format)."""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "user", "content": user_content}
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
            source="generate_briefing",
            timeout=60,
            context=ctx,
        ) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            for block in result["content"]:
                if block.get("type") == "text":
                    return block["text"].strip()
            return None
    except ai_provider_guard.ProviderCooldown as e:
        print(f"  MiniMax cooldown active, skipping briefing until {e.cooldown_until}")
        return None
    except urllib.error.HTTPError as e:
        print(f"  HTTP error: {e.code}")
        return None
    except Exception as e:
        print(f"  API error: {str(e)[:80]}")
        return None
    return None


def fetch_high_score_items(min_score=6, hours=24):
    """Fetch items from last N hours with relevance_score >= min_score."""
    if remote_db.feed_read_from_remote() or remote_db.app_state_to_remote():
        return remote_db.fetch_high_score_items_remote(min_score=min_score, hours=hours)
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT id, platform, source, title, ai_summary, ai_category, ai_keywords,
               relevance_score, author_name, url, fetched_at
        FROM items
        WHERE fetched_at > datetime('now', ?)
          AND relevance_score >= ?
        ORDER BY relevance_score DESC
    """, (f'-{hours} hours', min_score)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def prepare_input(items, user_ctx):
    """Prepare truncated input for the briefing prompt. Controls token budget."""
    # Group by category, max 5 per category
    by_cat = {}
    for it in items:
        cat = it.get('ai_category') or 'other'
        if cat not in by_cat:
            by_cat[cat] = []
        if len(by_cat[cat]) < 5:
            title = (it.get('title') or '')[:100]
            summary = (it.get('ai_summary') or '')[:150]
            keywords = ''
            kw_raw = it.get('ai_keywords')
            if kw_raw:
                if isinstance(kw_raw, str):
                    try:
                        kw_raw = json.loads(kw_raw)
                    except Exception:
                        kw_raw = []
                if isinstance(kw_raw, list):
                    keywords = ', '.join(kw_raw[:3])
            by_cat[cat].append({
                'id': it['id'],
                'title': title,
                'summary': summary,
                'platform': it.get('platform', ''),
                'score': it.get('relevance_score', 0),
                'keywords': keywords,
            })

    # Build text representation
    lines = []
    for cat, cat_items in by_cat.items():
        lines.append(f"\n## {cat} ({len(cat_items)} 条)")
        for ci in cat_items:
            lines.append(f"- [{ci['id']}] ({ci['platform']}, {ci['score']}分) {ci['title']}")
            if ci['summary']:
                lines.append(f"  摘要: {ci['summary']}")
            if ci['keywords']:
                lines.append(f"  关键词: {ci['keywords']}")

    # User context section
    ctx_lines = [
        "\n## 用户画像",
        f"角色: {user_ctx.get('role', '')}",
        f"目标: {', '.join(user_ctx.get('goals', []))}",
        f"项目: {', '.join(user_ctx.get('projects', []))}",
        f"关注领域: {user_ctx.get('interests', '')}",
    ]

    return '\n'.join(ctx_lines + [f"\n## 今日信息 ({len(items)} 条)"] + lines), len(items)


def build_system_prompt(interest_exclusions=None):
    """Build the system prompt for briefing generation.
    interest_exclusions: 用户已配置的兴趣关键词列表，AI 洞察应避开这些方向。
    """
    exclusion_text = ""
    if interest_exclusions:
        kw_str = ', '.join(interest_exclusions)
        exclusion_text = f"""重要约束：用户已通过兴趣模块主动关注以下方向：
{kw_str}
请避免在洞察中重复覆盖这些方向。你的任务是探索用户「兴趣之外」的盲区，发现他们可能忽略但有价值的信息。
"""

    from prompt_loader import load_prompt
    prompt = load_prompt('05_daily_briefing.md', exclusions=exclusion_text)
    if prompt:
        return prompt

    return f"""你是一位 AI 信息分析师，负责为用户生成信息洞察。

任务：基于用户画像和今日信息，生成聚合洞察和行动建议。
{exclusion_text}
要求：
1. 聚合洞察（insights）：将多条相关信息综合为 3-8 个洞察，每个洞察关联用户的一个目标维度
   - 不是逐条分析，而是跨信息源的综合判断
   - 每个洞察需要引用具体的源信息 ID
   - 标题要有观点，不要只是描述
   - 每个洞察必须附带 1 条行动建议（suggestion 字段，不可省略）
2. 行动建议（suggestions）：基于洞察和用户目标，推荐 2-5 个可执行行动
   - 每条建议说明与用户哪个目标/项目相关
   - 建议应该是具体可操作的

输出格式：严格 JSON，不要有其他内容（不要 markdown 代码块标记）。

{{
  "insights": [
    {{
      "id": "ins-1",
      "title": "洞察标题（有观点的一句话）",
      "summary": "详细分析（2-3句话，解释为什么这些信息放在一起看很重要）",
      "goal": "关联的用户目标",
      "source_ids": ["item_id_1", "item_id_2"],
      "suggestion": {{"title": "建议标题", "reason": "为什么推荐"}}
    }}
  ],
  "suggestions": [
    {{
      "id": "sug-1",
      "title": "建议标题（动词开头）",
      "reason": "为什么推荐这个行动（1-2句话，关联用户目标/项目）",
      "goal": "关联的用户目标"
    }}
  ]
}}"""


def parse_response(text):
    """Parse the JSON response, handling common issues."""
    if not text:
        return None
    # Strip markdown code block markers if present
    text = text.strip()
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
        # Validate structure
        if 'insights' not in data:
            data['insights'] = []
        if 'suggestions' not in data:
            data['suggestions'] = []
        return data
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Raw text (first 500 chars): {text[:500]}")
        return None


def main():
    config = load_config()
    ai_config = config.get("ai_summary", {})
    api_key, api_base, model = resolve_minimax_runtime_config(ai_config)
    provider = ai_config.get("provider", "minimax")

    if not api_key:
        print("ERROR: No MiniMax API key configured in .env or config.json")
        sys.exit(1)

    if provider == "minimax":
        try:
            ai_provider_guard.ensure_provider_available("minimax", source="generate_briefing.main")
        except ai_provider_guard.ProviderCooldown as e:
            print(f"MiniMax cooldown active, skipping briefing until {e.cooldown_until}")
            print(ai_provider_guard.cooldown_message("minimax"))
            return

    print("=" * 50)
    print("  AI 简报生成器")
    print("=" * 50)

    # Load user context
    user_ctx = load_user_context()
    print(f"用户: {user_ctx.get('role', 'unknown')}")
    print(f"目标: {', '.join(user_ctx.get('goals', []))}")

    # Fetch high-score items from last 24h
    items = fetch_high_score_items(min_score=6, hours=24)
    print(f"\n近 24h 高分内容: {len(items)} 条 (score >= 6)")

    if len(items) < 3:
        print("内容不足 3 条，跳过简报生成")
        return

    # v6.0: 加载兴趣关键词作为排除项
    interest_exclusions = []
    try:
        if remote_db.app_state_to_remote():
            interest_exclusions = remote_db.get_all_interest_keywords_remote()
        else:
            interest_exclusions = db.get_all_interest_keywords(db.get_conn())
        if interest_exclusions:
            print(f"兴趣排除关键词: {', '.join(interest_exclusions[:10])}{'...' if len(interest_exclusions) > 10 else ''}")
    except Exception as e:
        print(f"加载兴趣关键词失败: {e}")

    # Prepare input with token budget control
    user_content, input_count = prepare_input(items, user_ctx)
    system_prompt = build_system_prompt(interest_exclusions if interest_exclusions else None)

    print(f"输入预算: ~{len(user_content)} 字符")
    print("正在调用 AI 生成简报...")

    start_time = time.time()
    raw_response = call_minimax(api_key, api_base, model, system_prompt, user_content, max_tokens=4096)
    elapsed = time.time() - start_time

    if not raw_response:
        print("简报生成失败: API 返回空")
        return

    print(f"API 响应耗时: {elapsed:.1f}s")

    # Parse response
    data = parse_response(raw_response)
    if not data:
        print("简报生成失败: 无法解析 JSON")
        return

    insights = data.get('insights', [])
    suggestions = data.get('suggestions', [])
    print(f"生成洞察: {len(insights)} 条")
    print(f"生成建议: {len(suggestions)} 条")

    # Save to database
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    if remote_db.app_state_to_remote():
        remote_db.upsert_briefing_remote(today, today, insights, suggestions, input_count, model)
    else:
        conn = db.get_conn()
        db.upsert_briefing(conn, today, today, insights, suggestions, input_count, model)
        conn.close()

    print(f"\n简报已保存: {today}")
    for i, ins in enumerate(insights):
        print(f"  洞察 {i+1}: {ins.get('title', '(无标题)')}")
    for i, sug in enumerate(suggestions):
        print(f"  建议 {i+1}: {sug.get('title', '(无标题)')}")
    print(f"\n完成!")


if __name__ == "__main__":
    main()
