#!/usr/bin/env python3
"""
v8.0 — Action Point Generation Engine (generate_actions.py)

Five-dimension AI semantic analysis on incoming items → actionable action points.
Triggered by fetch_all.sh after each fetch cycle.

Dimensions: relevance, actionability, timeliness, incremental value, ROI.
Uses MiniMax API with batch processing (10 items/batch, 20 concurrency).
Reads ~/claudecode_workspace/WORKSPACE-MANIFEST.md as user context.

Usage:
  python3 generate_actions.py                # incremental (new items since last run)
  python3 generate_actions.py --cold-start   # analyze all history items
  python3 generate_actions.py --limit 50     # limit items to analyze
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import ai_provider_guard
import db
from env_utils import load_project_env

CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")
MANIFEST_PATH = os.path.expanduser("~/claudecode_workspace/WORKSPACE-MANIFEST.md")
PULSE_PATH = os.path.expanduser("~/claudecode_workspace/WORKSPACE-PULSE.json")
PROMPT_TEMPLATE_PATH = os.path.join(BASE_DIR, "prompts", "04_action_analysis.md")
ACTION_LOG_DIR = os.path.join(BASE_DIR, "logs", "action_generation")

DIRECTIONS_PATH = os.path.join(BASE_DIR, "config", "directions.yaml")
BATCH_SIZE = 10
# Read from config.json; fallback defaults
def _get_concurrency_settings():
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        ai = cfg.get('ai_summary', {})
        return ai.get('max_concurrency', 3), ai.get('request_interval', 1.0)
    except Exception:
        return 3, 1.0

MAX_CONCURRENCY, REQUEST_INTERVAL = _get_concurrency_settings()

_SSL_CTX = ssl.create_default_context()
_DEFAULT_MINIMAX_CHAT_BASE = "https://api.minimaxi.com/anthropic/v1"
_DEFAULT_MINIMAX_CHAT_MODEL = "MiniMax-M3"
_AUTH_HTTP_STATUS = {401, 403}

# M3 默认用英文思考;实测单靠 system prompt 约束无效,必须在 user 内容首尾各夹一道
# 中文强指令才生效(recency + 首因双重锚定)。专有名词保留原文。
CHINESE_ONLY_PREFIX = "【全程只用简体中文进行思考(thinking)与输出所有字段，禁止出现英文句子；MiniMax/GitHub 等专有名词可保留原文】\n\n"
CHINESE_ONLY_SUFFIX = "\n\n【再次强调：你的 thinking 过程和最终输出的每个字段都必须是简体中文，不要用英文。】"


class ProviderAuthenticationError(RuntimeError):
    """Raised when the provider rejects credentials.

    Auth failures must not be treated as an empty model result, otherwise
    manual action generation can silently persist a fallback action.
    """


def _read_http_error_body(exc, limit=200):
    try:
        return exc.read().decode("utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def _raise_auth_error(exc, *, source):
    raise ProviderAuthenticationError(
        f"MiniMax authentication failed (HTTP {exc.code}) in {source}. "
        "Check MINIMAX_API_KEY in .env."
    ) from exc


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def resolve_minimax_chat_config(ai_config):
    """Resolve MiniMax chat credentials shared by scripts and route handlers."""
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


def load_manifest():
    """Load WORKSPACE-MANIFEST.md as user context. Gracefully degrade if missing."""
    try:
        with open(MANIFEST_PATH, "r") as f:
            return f.read()
    except FileNotFoundError:
        print(f"[warn] WORKSPACE-MANIFEST.md not found at {MANIFEST_PATH}, proceeding without context")
        return ""


def load_pulse():
    """Load WORKSPACE-PULSE.json and extract fields for prompt injection.
    Returns dict with keys: pulse_active_work, pulse_problems, pulse_learnings, pulse_content.
    """
    try:
        with open(PULSE_PATH, "r") as f:
            pulse = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[warn] WORKSPACE-PULSE.json load failed: {e}, proceeding without pulse")
        return {"pulse_active_work": "", "pulse_problems": "", "pulse_learnings": "", "pulse_content": ""}

    # active_work: project + focus + context + blockers
    active_parts = []
    for w in pulse.get("active_work", []):
        lines = [f"- **{w.get('project', '?')}**: {w.get('focus', '')} ({w.get('status', '')})"]
        if w.get("context"):
            lines.append(f"  上下文: {w['context']}")
        for b in w.get("blockers", []):
            lines.append(f"  卡点: {b}")
        active_parts.append("\n".join(lines))

    # problems: pain_points + unsolved_questions + all blockers
    problem_parts = []
    for p in pulse.get("pain_points", []):
        problem_parts.append(f"- [{p.get('severity', 'medium')}] {p.get('description', '')}")
        if p.get("still_unsolved"):
            problem_parts.append(f"  未解决: {p['still_unsolved']}")
    for q in pulse.get("unsolved_questions", []):
        problem_parts.append(f"- [question] {q}")

    # learnings: recent_learnings + tools_evaluated
    learning_parts = []
    for l in pulse.get("recent_learnings", []):
        learning_parts.append(f"- {l.get('topic', '')}: {l.get('insight', '')}")
    for t in pulse.get("tools_evaluated", []):
        status = "持续关注" if t.get("keep_watching") else "已定论"
        learning_parts.append(f"- [工具/{status}] {t.get('tool', '')}: {t.get('verdict', '')}")

    # content: content_pipeline + itch_list
    content_parts = []
    for c in pulse.get("content_pipeline", []):
        content_parts.append(f"- {c.get('topic', '')} ({c.get('stage', '')}): {c.get('angle', '')}")
    for itch in pulse.get("itch_list", []):
        content_parts.append(f"- [想法] {itch}")

    return {
        "pulse_active_work": "\n".join(active_parts),
        "pulse_problems": "\n".join(problem_parts),
        "pulse_learnings": "\n".join(learning_parts),
        "pulse_content": "\n".join(content_parts),
    }


def load_directions():
    """Load directions framework from YAML. Returns (directions_dict, directions_text_for_prompt)."""
    try:
        import yaml
    except ImportError:
        yaml = None

    try:
        with open(DIRECTIONS_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        if yaml:
            data = yaml.safe_load(raw)
        else:
            # Fallback: parse YAML-like structure manually
            import re
            data = {'directions': {}}
            current_slug = None
            for line in raw.split('\n'):
                m = re.match(r'^  (\w[\w-]*):', line)
                if m:
                    current_slug = m.group(1)
                    data['directions'][current_slug] = {}
                elif current_slug:
                    m2 = re.match(r'^\s+label:\s*"?([^"]+)"?', line)
                    if m2:
                        data['directions'][current_slug]['label'] = m2.group(1).strip()
                    m3 = re.match(r'^\s+description:\s*"?([^"]+)"?', line)
                    if m3:
                        data['directions'][current_slug]['description'] = m3.group(1).strip()

        dirs = data.get('directions', {})

        # Build text representation for prompt injection
        lines = ["## 行动方向框架\n"]
        lines.append("每个行动点必须归属一个方向。从以下方向中选择，或提议新方向。\n")
        for slug, info in dirs.items():
            label = info.get('label', slug)
            desc = info.get('description', '')
            scope = info.get('scope', [])
            not_scope = info.get('not_scope', [])
            lines.append(f"### `{slug}` — {label}")
            lines.append(f"定义：{desc}")
            if scope:
                lines.append("属于此方向：" + "；".join(scope))
            if not_scope:
                lines.append("不属于此方向：" + "；".join(not_scope))
            lines.append("")

        return dirs, "\n".join(lines)
    except Exception as e:
        print(f"[warn] Failed to load directions: {e}")
        return {}, ""


def save_generation_log(log_data):
    """Save a full action generation log for debugging and traceability."""
    os.makedirs(ACTION_LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = log_data.get("mode", "batch")
    path = os.path.join(ACTION_LOG_DIR, f"{ts}_{mode}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2, default=str)
        print(f"[log] 行动生成日志已保存: {path}")
        # Keep only last 50 logs
        logs = sorted([f for f in os.listdir(ACTION_LOG_DIR) if f.endswith('.json')])
        for old in logs[:-50]:
            os.remove(os.path.join(ACTION_LOG_DIR, old))
    except Exception as e:
        print(f"[warn] 保存日志失败: {e}")
    return path


def call_minimax(api_key, api_base, model, system_prompt, content, max_tokens=4096):
    """Call MiniMax API (Anthropic-compatible format)."""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content[:16000]}]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    })

    try:
        with ai_provider_guard.guarded_urlopen(
            req,
            source="generate_actions",
            timeout=90,
            context=_SSL_CTX,
        ) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
            return None
    except ai_provider_guard.ProviderCooldown:
        raise
    except urllib.error.HTTPError as e:
        err_body = _read_http_error_body(e)
        print(f"[error] MiniMax HTTP {e.code}: {err_body}")
        if e.code in _AUTH_HTTP_STATUS:
            _raise_auth_error(e, source="generate_actions")
        if e.code == 429:
            ai_provider_guard.ensure_provider_available("minimax", source="generate_actions")
        return None
    except Exception as e:
        print(f"[error] MiniMax call failed: {e}")
        return None
    return None


def call_minimax_streaming(api_key, api_base, model, system_prompt, content, max_tokens=4096, on_thinking=None):
    """Call MiniMax API with streaming + thinking enabled. Returns text result.
    on_thinking(text): callback for each thinking chunk from the model."""
    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": system_prompt,
        "max_tokens": max_tokens,
        "stream": True,
        "thinking": {"type": "enabled", "budget_tokens": 10000},
        "messages": [{"role": "user", "content": "请用中文进行所有思考和分析。\n\n" + content[:16000]}]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    })

    for attempt in range(3):
        try:
            resp = ai_provider_guard.guarded_urlopen(
                req,
                source="generate_actions_streaming",
                timeout=120,
                context=_SSL_CTX,
            )
            result_text = ""
            thinking_buf = ""

            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except (json.JSONDecodeError, TypeError):
                    continue

                evt_type = evt.get("type", "")

                # Handle content_block_delta events
                if evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    delta_type = delta.get("type", "")
                    if delta_type == "thinking_delta":
                        chunk = delta.get("thinking", "")
                        if chunk and on_thinking:
                            thinking_buf += chunk
                            # Flush on sentence boundaries
                            while "\n" in thinking_buf:
                                line_text, thinking_buf = thinking_buf.split("\n", 1)
                                line_text = line_text.strip()
                                if line_text:
                                    on_thinking(line_text)
                    elif delta_type == "text_delta":
                        result_text += delta.get("text", "")

                # Handle message_stop
                elif evt_type == "message_stop":
                    break

            # Flush remaining thinking buffer
            if thinking_buf.strip() and on_thinking:
                on_thinking(thinking_buf.strip())

            resp.close()
            return result_text.strip() if result_text.strip() else None

        except ai_provider_guard.ProviderCooldown:
            raise
        except urllib.error.HTTPError as e:
            err_body = _read_http_error_body(e)
            print(f"[error] MiniMax streaming HTTP {e.code}: {err_body}")
            if e.code in _AUTH_HTTP_STATUS:
                _raise_auth_error(e, source="generate_actions_streaming")
            if e.code == 429:
                ai_provider_guard.ensure_provider_available("minimax", source="generate_actions_streaming")
            # Fallback to non-streaming if streaming not supported
            if e.code in (400, 422):
                print("[warn] Streaming/thinking not supported, falling back to non-streaming")
                return call_minimax(api_key, api_base, model, system_prompt, content, max_tokens)
            return None
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                req = urllib.request.Request(url, data=payload, headers={
                    "x-api-key": api_key, "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json"
                })
                continue
            print(f"[error] MiniMax streaming call failed: {e}")
            # Fallback to non-streaming
            return call_minimax(api_key, api_base, model, system_prompt, content, max_tokens)
    return None


def _load_prompt_template():
    """Load prompt template from external .md file."""
    try:
        with open(PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # Strip the frontmatter (everything before first ---)
        # The actual prompt starts after the "---" separator line
        parts = content.split("\n---\n", 1)
        return parts[1].strip() if len(parts) > 1 else content.strip()
    except FileNotFoundError:
        print(f"[WARN] Prompt template not found: {PROMPT_TEMPLATE_PATH}, using fallback")
        return None


def build_analysis_prompt(manifest_text, feedback_context="", directions_text="", pulse_fields=None, user_guidance=""):
    """Build the five-dimension analysis system prompt from template file.

    pulse_fields: dict with keys pulse_active_work, pulse_problems, pulse_learnings, pulse_content.
    user_guidance: optional string with user's generation preferences (action_type, hint).
    """
    template = _load_prompt_template()
    if template is None:
        return f"分析以下信息条目，生成行动点。用户上下文：{manifest_text or '无'}"

    prompt = template.replace("{manifest_text}", manifest_text or '（未提供用户上下文）')
    prompt = prompt.replace("{feedback_context}", feedback_context or "")
    prompt = prompt.replace("{directions_text}", directions_text or "")
    prompt = prompt.replace("{user_guidance}", user_guidance or "（无特殊偏好，按标准规则生成）")

    # Inject PULSE fields
    pf = pulse_fields or {}
    prompt = prompt.replace("{pulse_active_work}", pf.get("pulse_active_work", "（无实时状态）"))
    prompt = prompt.replace("{pulse_problems}", pf.get("pulse_problems", "（无）"))
    prompt = prompt.replace("{pulse_learnings}", pf.get("pulse_learnings", "（无）"))
    prompt = prompt.replace("{pulse_content}", pf.get("pulse_content", "（无）"))
    return prompt


def build_item_content(items):
    """Build content string for a batch of items."""
    # 在 batch 开头列出 ID 清单，约束 LLM 只能从中选择
    if len(items) > 1:
        id_list = "\n".join(f"  - {it['id']}" for it in items)
        parts = [f"""【本批次包含 {len(items)} 条信息，ID 清单如下】
{id_list}
⚠️ 生成行动点时，source_item_ids 必须且只能从上述 ID 中选择。每个行动点的 source_item_ids 必须精确对应其内容来源，不得张冠李戴。
"""]
    else:
        parts = []
    for it in items:
        refs = ""
        if it.get('detail_json'):
            try:
                dj = json.loads(it['detail_json']) if isinstance(it['detail_json'], str) else it['detail_json']
                urls = dj.get('referenced_urls', [])
                if urls:
                    ref_parts = []
                    for u in urls[:5]:
                        line = f"  - {u.get('title', u.get('url', ''))}: {u.get('description', '')}"
                        # v8.0.3: 包含 full_text 供 AI 深度分析
                        ft = u.get('full_text', '')
                        if ft:
                            line += f"\n    全文: {ft[:3000]}"
                        ref_parts.append(line)
                    refs = "\n引用链接:\n" + "\n".join(ref_parts)
            except (json.JSONDecodeError, TypeError):
                pass

        parts.append(f"""---
ID: {it['id']}
平台: {it.get('platform', '')}
标题: {it.get('title', '')}
原文: {(it.get('content', '') or '')[:3000]}
AI摘要: {it.get('ai_summary', '') or ''}
关键要点: {it.get('ai_key_points', '') or ''}
分类: {it.get('ai_category', '') or ''}{refs}
""")
    return "\n".join(parts)


def build_feedback_context(conn):
    """Build feedback context string from recent user signals."""
    parts = []

    # Recent dismissed actions
    dismissed = db.get_recent_dismissed_actions(conn, limit=5)
    if dismissed:
        parts.append("## 用户历史反馈\n### 最近被忽略的行动点：")
        for d in dismissed:
            detail = ""
            if d.get('detail_json'):
                try:
                    det = json.loads(d['detail_json'])
                    detail = f" — 原因: {det.get('feedback_type', '')} {det.get('feedback_text', '')}"
                except (json.JSONDecodeError, TypeError):
                    pass
            parts.append(f"- {d['title']}{detail}")

    # Recent edited actions (diff)
    edited = db.get_recent_edited_actions(conn, limit=5)
    if edited:
        parts.append("\n### 最近被编辑的行动点（用户修改了 AI 生成的内容）：")
        for e in edited:
            diffs = []
            if e['original_title'] != e['title']:
                diffs.append(f"标题: '{e['original_title']}' → '{e['title']}'")
            if e['original_priority'] != e['priority']:
                diffs.append(f"优先级: {e['original_priority']} → {e['priority']}")
            if diffs:
                parts.append(f"- {', '.join(diffs)}")

    # Recent explicit feedback
    feedback = db.get_recent_action_feedback(conn, limit=5)
    if feedback:
        parts.append("\n### 最近的显式反馈：")
        for f in feedback:
            parts.append(f"- [{f['phase']}] {f['title']}: {f['rating']}" +
                         (f" — {f['comment']}" if f.get('comment') else ""))

    return "\n".join(parts) if parts else ""


def get_items_to_analyze(conn, cold_start=False, limit=500, offset=0, skip_existing=False):
    """Get items to analyze. Incremental: items from last 2 hours. Cold start: all."""
    skip_clause = ""
    if skip_existing:
        # Get item IDs that already have actions
        skip_clause = """
              AND id NOT IN (
                  SELECT DISTINCT json_each.value
                  FROM actions, json_each(actions.source_item_ids)
              )"""

    if cold_start:
        rows = conn.execute(f"""
            SELECT id, platform, title, content, ai_summary, ai_key_points,
                   ai_category, detail_json
            FROM items
            WHERE ai_summary IS NOT NULL AND ai_summary != ''
            {skip_clause}
            ORDER BY fetched_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT id, platform, title, content, ai_summary, ai_key_points,
                   ai_category, detail_json
            FROM items
            WHERE ai_summary IS NOT NULL AND ai_summary != ''
              AND fetched_at >= datetime('now', '-2 hours')
            {skip_clause}
            ORDER BY fetched_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_existing_pending_actions(conn):
    """Get existing pending actions for dedup."""
    rows = conn.execute("""
        SELECT id, title, prompt, reason, related_project, source_item_ids
        FROM actions WHERE status IN ('pending', 'confirmed')
    """).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        val = d.get('source_item_ids')
        if val and isinstance(val, str):
            try:
                d['source_item_ids'] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d['source_item_ids'] = []
        results.append(d)
    return results


def _substring_dedup(new_actions, existing_actions):
    """Fallback dedup: skip new actions whose title is very similar to existing ones."""
    if not existing_actions:
        return new_actions

    existing_titles = set()
    for ea in existing_actions:
        t = (ea.get('title') or '').lower().strip()
        if t:
            existing_titles.add(t)

    deduped = []
    for na in new_actions:
        nt = (na.get('title') or '').lower().strip()
        skip = False
        for et in existing_titles:
            if nt == et or (len(nt) > 10 and nt in et) or (len(et) > 10 and et in nt):
                skip = True
                break
        if not skip:
            deduped.append(na)
            existing_titles.add(nt)
    return deduped


DEDUP_BATCH_SIZE = 50


def _build_dedup_prompt(existing_actions, new_actions):
    """Build the semantic dedup prompt for MiniMax."""
    existing_parts = []
    for ea in existing_actions:
        existing_parts.append(
            f"- ID: {ea['id']}\n  标题: {ea.get('title', '')}\n"
            f"  Prompt: {(ea.get('prompt') or '')[:500]}\n"
            f"  原因: {(ea.get('reason') or '')[:300]}"
        )

    new_parts = []
    for i, na in enumerate(new_actions):
        new_parts.append(
            f"- Index: {i}\n  标题: {na.get('title', '')}\n"
            f"  Prompt: {(na.get('prompt') or '')[:500]}\n"
            f"  原因: {(na.get('reason') or '')[:300]}"
        )

    existing_text = chr(10).join(existing_parts)
    new_text = chr(10).join(new_parts)

    from prompt_loader import load_prompt
    prompt = load_prompt('04b_action_dedup.md',
                         existing_actions=existing_text, new_actions=new_text)
    if prompt:
        return prompt

    return f"""你是一个行动点去重引擎。以下有两组行动点：

## 已有行动点（pending 池）
{existing_text}

## 新生成的行动点
{new_text}

请判断新行动点中，哪些与已有行动点指向同一个优化方向/功能点。

判断标准：
- 同一个优化方向 = 最终要达成的目标相同（即使描述方式不同）
- 不同工具/方案但指向同一目标 = 应合并
- 仅主题相关但目标不同 = 不合并

输出严格 JSON（不要加 markdown 代码块）：
{{"merge": [{{"new_index": 0, "existing_id": "uuid", "reason": "都指向 XX 优化"}}], "new": [1, 2, 3]}}

merge: 需要合并的新行动点，new_index 是新行动点的 index，existing_id 是应合并到的已有行动点 ID
new: 全新的行动点 index 列表（不与任何已有行动点重复）

注意：每个新行动点必须出现在 merge 或 new 中，不能遗漏。"""


def _parse_dedup_response(text, new_count):
    """Parse dedup AI response. Returns (merge_list, new_indices) or None on failure."""
    if not text:
        return None
    text = text.strip()
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

    if not isinstance(data, dict):
        return None

    merge_list = data.get('merge', [])
    new_indices = data.get('new', [])

    # Validate
    if not isinstance(merge_list, list) or not isinstance(new_indices, list):
        return None

    # Ensure all indices are covered
    covered = set()
    for m in merge_list:
        if isinstance(m, dict) and 'new_index' in m and 'existing_id' in m:
            covered.add(m['new_index'])
    for idx in new_indices:
        if isinstance(idx, int):
            covered.add(idx)

    # If coverage is incomplete, treat uncovered as new
    all_indices = set(range(new_count))
    missing = all_indices - covered
    if missing:
        new_indices = list(set(new_indices) | missing)

    return merge_list, new_indices


def semantic_deduplicate(conn, new_actions, existing_actions, api_key, api_base, model):
    """Semantic dedup using MiniMax AI. Falls back to substring matching on failure.

    Returns:
        list: new actions that should be inserted (not merged).
        Merged actions are updated in DB directly.
    """
    if not existing_actions:
        return new_actions

    if not new_actions:
        return []

    # Process existing actions in batches of DEDUP_BATCH_SIZE
    all_merge_instructions = []  # (new_index, existing_id, reason)
    new_indices_set = set(range(len(new_actions)))  # Start assuming all are new

    existing_batches = [existing_actions[i:i + DEDUP_BATCH_SIZE]
                        for i in range(0, len(existing_actions), DEDUP_BATCH_SIZE)]

    for batch_idx, existing_batch in enumerate(existing_batches):
        prompt_content = _build_dedup_prompt(existing_batch, new_actions)

        try:
            result = call_minimax(
                api_key, api_base, model,
                "你是行动点去重引擎，严格输出 JSON。",
                prompt_content,
                max_tokens=4096
            )
            parsed = _parse_dedup_response(result, len(new_actions))
            if parsed is None:
                print(f"[warn] Semantic dedup batch {batch_idx + 1}: failed to parse response, skipping batch")
                continue

            merge_list, batch_new_indices = parsed

            for m in merge_list:
                if isinstance(m, dict) and 'new_index' in m and 'existing_id' in m:
                    idx = m['new_index']
                    eid = m['existing_id']
                    # Verify existing_id is actually in this batch
                    valid_ids = {ea['id'] for ea in existing_batch}
                    if eid in valid_ids and idx in new_indices_set:
                        all_merge_instructions.append((idx, eid, m.get('reason', '')))
                        new_indices_set.discard(idx)

        except (ai_provider_guard.ProviderCooldown, ProviderAuthenticationError):
            raise
        except Exception as e:
            print(f"[warn] Semantic dedup batch {batch_idx + 1} failed: {e}")
            continue

    # If semantic dedup found nothing (all batches failed), fall back to substring
    if not all_merge_instructions and len(existing_batches) > 0:
        had_any_success = len(new_indices_set) < len(new_actions)
        if not had_any_success:
            print("[warn] Semantic dedup returned no merges across all batches, falling back to substring dedup")
            return _substring_dedup(new_actions, existing_actions)

    # Execute merges in DB
    merged_count = 0
    for new_idx, existing_id, merge_reason in all_merge_instructions:
        try:
            na = new_actions[new_idx]
            ea = next((e for e in existing_actions if e['id'] == existing_id), None)
            if not ea:
                new_indices_set.add(new_idx)
                continue

            # Merge source_item_ids
            existing_ids = ea.get('source_item_ids', []) or []
            if isinstance(existing_ids, str):
                try:
                    existing_ids = json.loads(existing_ids)
                except (json.JSONDecodeError, TypeError):
                    existing_ids = []
            new_src_ids = na.get('source_item_ids', []) or []
            merged_ids = list(dict.fromkeys(existing_ids + new_src_ids))  # dedupe, preserve order
            merged_ids_json = json.dumps(merged_ids, ensure_ascii=False)

            # Update prompt: append new info
            existing_prompt = ea.get('prompt', '') or ''
            new_prompt = na.get('prompt', '') or ''
            if new_prompt and new_prompt not in existing_prompt:
                updated_prompt = existing_prompt.rstrip() + f"\n\n[补充信息源] {new_prompt}"
            else:
                updated_prompt = existing_prompt

            # Update reason
            new_reason_text = na.get('reason', '') or ''
            existing_reason = ea.get('reason', '') or ''
            if new_reason_text and new_reason_text not in existing_reason:
                updated_reason = existing_reason.rstrip() + f" | 补充: {new_reason_text}"
            else:
                updated_reason = existing_reason

            db.update_action(conn, existing_id,
                             source_item_ids=merged_ids_json,
                             prompt=updated_prompt,
                             reason=updated_reason)

            # Log merge event
            db._log_action_event(conn, existing_id, 'merged', {
                'merged_new_title': na.get('title', ''),
                'merged_source_item_ids': new_src_ids,
                'merge_reason': merge_reason,
            })
            merged_count += 1

        except Exception as e:
            print(f"[warn] Failed to merge action index {new_idx} into {existing_id}: {e}")
            new_indices_set.add(new_idx)

    if merged_count > 0:
        print(f"[info] Semantic dedup: merged {merged_count} actions into existing ones")

    # Return only the truly new actions
    return [new_actions[i] for i in sorted(new_indices_set)]


def parse_actions_response(text):
    """Parse AI response into list of action dicts."""
    if not text:
        return []
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```\w*\n?', '', text)
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and 'actions' in data:
            return data['actions']
        return []
    except json.JSONDecodeError:
        # Try to extract JSON array from text
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        # BF-0706-5: 抢救被截断/畸形的数组 —— 逐个抽出完整的顶层 {..} action 对象,
        # 丢弃末尾没闭合的那个(M2/M3 思考+输出超 max_tokens 被截断时高发)。
        salvaged = _salvage_action_objects(text)
        if salvaged:
            print(f"[warn] parsed via salvage: {len(salvaged)} action(s) from malformed JSON")
            return salvaged
        print(f"[warn] Could not parse actions response: {text[:200]}")
        return []


def _salvage_action_objects(text):
    """从畸形/截断的 JSON 文本里抽出完整的顶层 {..} 对象(在最外层数组内),逐个 json.loads。

    追踪字符串态 + 花括号深度;末尾没闭合的对象自然被跳过。对未转义引号也尽量容错
    (坏片段 json.loads 失败即跳过),不抛异常。"""
    start = text.find('[')
    if start < 0:
        start = text.find('{') - 1  # 没有数组包裹时也尝试从第一个对象抽
    objs = []
    depth = 0
    in_str = False
    esc = False
    obj_start = None
    for i in range(start + 1, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                if depth == 0:
                    obj_start = i
                depth += 1
            elif c == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and obj_start is not None:
                        try:
                            objs.append(json.loads(text[obj_start:i + 1]))
                        except (json.JSONDecodeError, TypeError):
                            pass
                        obj_start = None
    return objs


def process_batch(batch_items, api_key, api_base, model, system_prompt):
    """Process one batch of items. Returns list of action dicts (including scores if present)."""
    content = build_item_content(batch_items)
    result = call_minimax(api_key, api_base, model, system_prompt, content, max_tokens=8192)
    actions = parse_actions_response(result)

    # 验证 source_item_ids：只保留本 batch 内的有效 ID
    valid_ids = {it['id'] for it in batch_items}
    for action in actions:
        src_ids = action.get('source_item_ids', [])
        if src_ids:
            filtered = [sid for sid in src_ids if sid in valid_ids]
            if len(filtered) != len(src_ids):
                invalid = set(src_ids) - valid_ids
                print(f"[warn] Action '{action.get('title', '')[:50]}' has invalid source_item_ids: {invalid}", flush=True)
            action['source_item_ids'] = filtered

    return actions


def process_single_item(item, api_key, api_base, model, system_prompt):
    """Process a single item for on-demand generation. Returns (action_dict_or_None, scores_dict_or_None, log_path_or_None).
    Unlike batch mode, this preserves scores even when the action is filtered out by threshold."""
    item_id = item.get('id', '?')
    item_title = (item.get('title', '') or '')[:80]
    print(f"[single-item] 开始分析 item_id={item_id} title={item_title}")
    content = build_item_content([item])
    single_mode_prompt = system_prompt + "\n\n【重要】这是用户手动选择的单条信息，请务必给出行动点和评分，即使你认为价值不高。不得返回空数组。"
    result = call_minimax(api_key, api_base, model, single_mode_prompt, content)
    print(f"[single-item] MiniMax 响应 item_id={item_id}: {(result or '(None)')[:500]}")
    all_parsed = parse_actions_response(result)

    # Save generation log
    log_path = save_generation_log({
        "mode": "single",
        "timestamp": datetime.now().isoformat(),
        "item": {"id": item_id, "title": item.get('title', ''), "platform": item.get('platform', '')},
        "context_loaded": {
            "system_prompt_length": len(single_mode_prompt),
            "item_content_length": len(content),
        },
        "system_prompt": single_mode_prompt,
        "item_content": content,
        "model_raw_response": result,
        "parsed_actions": all_parsed,
        "result": "generated" if all_parsed else "filtered_or_empty",
    })

    if all_parsed and len(all_parsed) > 0:
        a = all_parsed[0]
        scores = a.get('scores')
        return a, scores, log_path
    # AI 返回了空数组 — 尝试从原始文本中提取 scores（可能评分低于阈值被 AI 过滤）
    # 尝试解析原始 JSON 找 scores
    if result:
        raw_text = result.strip()
        if raw_text.startswith('```'):
            raw_text = re.sub(r'^```\w*\n?', '', raw_text)
        if raw_text.endswith('```'):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()
        # 如果是空数组 [] 说明 AI 主动过滤了
        if raw_text == '[]':
            return None, None, log_path
        # 尝试解析可能的低分条目
        try:
            data = json.loads(raw_text)
            if isinstance(data, list) and len(data) > 0:
                return None, data[0].get('scores'), log_path
            if isinstance(data, dict):
                return None, data.get('scores'), log_path
        except (json.JSONDecodeError, TypeError):
            pass
    return None, None, log_path


def process_single_item_streaming(item, api_key, api_base, model, system_prompt, on_thinking=None):
    """Like process_single_item but uses streaming API with thinking output.
    on_thinking(text): callback for each thinking line from MiniMax."""
    item_id = item.get('id', '?')
    item_title = (item.get('title', '') or '')[:80]
    print(f"[single-item-stream] 开始分析 item_id={item_id} title={item_title}")
    content = build_item_content([item])
    single_mode_prompt = system_prompt + "\n\n【重要】这是用户手动选择的单条信息，请务必给出行动点和评分，即使你认为价值不高。不得返回空数组。"
    # v2: M3 默认英文思考,单靠 system 约束无效;必须在 user 内容首尾夹中文强指令(实测有效)
    wrapped_content = CHINESE_ONLY_PREFIX + content + CHINESE_ONLY_SUFFIX
    # BF-0706-5: M3 是推理模型,思考也吃 max_tokens;6144 时思考+深度输出会超额度 → JSON 被截断走兜底。放宽。
    result = call_minimax_streaming(api_key, api_base, model, single_mode_prompt, wrapped_content, max_tokens=12000, on_thinking=on_thinking)
    print(f"[single-item-stream] MiniMax 响应 item_id={item_id}: {(result or '(None)')[:500]}")
    all_parsed = parse_actions_response(result)

    log_path = save_generation_log({
        "mode": "single_streaming",
        "timestamp": datetime.now().isoformat(),
        "item": {"id": item_id, "title": item.get('title', ''), "platform": item.get('platform', '')},
        "context_loaded": {
            "system_prompt_length": len(single_mode_prompt),
            "item_content_length": len(content),
        },
        "system_prompt": single_mode_prompt,
        "item_content": content,
        "model_raw_response": result,
        "parsed_actions": all_parsed,
        "result": "generated" if all_parsed else "filtered_or_empty",
    })

    if all_parsed and len(all_parsed) > 0:
        a = all_parsed[0]
        scores = a.get('scores')
        return a, scores, log_path
    if result:
        raw_text = result.strip()
        if raw_text.startswith('```'):
            raw_text = re.sub(r'^```\w*\n?', '', raw_text)
        if raw_text.endswith('```'):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()
        if raw_text == '[]':
            return None, None, log_path
        try:
            data = json.loads(raw_text)
            if isinstance(data, list) and len(data) > 0:
                return None, data[0].get('scores'), log_path
            if isinstance(data, dict):
                return None, data.get('scores'), log_path
        except (json.JSONDecodeError, TypeError):
            pass
    return None, None, log_path


def main():
    parser = argparse.ArgumentParser(description="Generate action points from items")
    parser.add_argument("--cold-start", action="store_true", help="Analyze all history items")
    parser.add_argument("--limit", type=int, default=500, help="Max items to analyze")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N items (for batch pagination)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip items that already have actions")
    args = parser.parse_args()

    cfg = load_config()
    ai = cfg.get('ai_summary', {})
    api_key, api_base, model = resolve_minimax_chat_config(ai)
    provider = ai.get('provider', 'minimax')

    if not api_key:
        print("[error] No MiniMax API key configured in .env or config.json")
        sys.exit(1)

    if provider == "minimax":
        try:
            ai_provider_guard.ensure_provider_available("minimax", source="generate_actions.main")
        except ai_provider_guard.ProviderCooldown as e:
            print(f"[info] MiniMax cooldown active, skipping action generation until {e.cooldown_until}")
            print(f"[info] {ai_provider_guard.cooldown_message('minimax')}")
            return

    # Load context
    manifest = load_manifest()
    pulse_fields = load_pulse()
    directions, directions_text = load_directions()
    conn = db.get_conn()
    feedback_ctx = build_feedback_context(conn)

    # Get items
    items = get_items_to_analyze(conn, cold_start=args.cold_start, limit=args.limit,
                                  offset=args.offset, skip_existing=args.skip_existing)
    if not items:
        print("[info] No items to analyze")
        conn.close()
        return

    print(f"[info] Analyzing {len(items)} items {'(cold start)' if args.cold_start else '(incremental)'}", flush=True)

    # Build prompt
    system_prompt = build_analysis_prompt(manifest, feedback_ctx, directions_text, pulse_fields)

    # Split into batches
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    all_actions = []
    failed_batches = []

    # Process batches with controlled concurrency
    workers = min(MAX_CONCURRENCY, len(batches))
    print(f"[info] Processing {len(batches)} batches, concurrency={workers}, interval={REQUEST_INTERVAL}s", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {}
        for i, batch in enumerate(batches):
            f = executor.submit(process_batch, batch, api_key, api_base, model, system_prompt)
            futures[f] = i
            if i < len(batches) - 1:
                time.sleep(REQUEST_INTERVAL)

        for f in as_completed(futures):
            batch_idx = futures[f]
            try:
                batch_actions = f.result()
                if batch_actions:
                    all_actions.extend(batch_actions)
                    print(f"  Batch {batch_idx + 1}/{len(batches)}: {len(batch_actions)} actions", flush=True)
                else:
                    print(f"  Batch {batch_idx + 1}/{len(batches)}: 0 actions", flush=True)
            except Exception as e:
                if isinstance(e, ai_provider_guard.ProviderCooldown):
                    print(f"  Batch {batch_idx + 1}/{len(batches)}: COOLDOWN — {e}", flush=True)
                    conn.close()
                    return
                if isinstance(e, ProviderAuthenticationError):
                    print(f"  Batch {batch_idx + 1}/{len(batches)}: AUTH ERROR — {e}", flush=True)
                    conn.close()
                    return
                print(f"  Batch {batch_idx + 1}/{len(batches)}: FAILED — {e}", flush=True)
                failed_batches.append(batch_idx)

    # Retry failed batches with lower concurrency
    if failed_batches:
        print(f"[info] Retrying {len(failed_batches)} failed batches...", flush=True)
        for batch_idx in failed_batches:
            try:
                batch_actions = process_batch(
                    batches[batch_idx], api_key, api_base, model, system_prompt
                )
                if batch_actions:
                    all_actions.extend(batch_actions)
                    print(f"  Retry batch {batch_idx + 1}: {len(batch_actions)} actions", flush=True)
            except Exception as e:
                if isinstance(e, ai_provider_guard.ProviderCooldown):
                    print(f"  Retry batch {batch_idx + 1}: COOLDOWN — {e}", flush=True)
                    conn.close()
                    return
                if isinstance(e, ProviderAuthenticationError):
                    print(f"  Retry batch {batch_idx + 1}: AUTH ERROR — {e}", flush=True)
                    conn.close()
                    return
                print(f"  Retry batch {batch_idx + 1}: FAILED — {e}", flush=True)

    if not all_actions:
        print("[info] No action points generated")
        conn.close()
        return

    # Deduplicate with existing pending actions (semantic + fallback to substring)
    existing = get_existing_pending_actions(conn)
    try:
        deduped = semantic_deduplicate(conn, all_actions, existing, api_key, api_base, model)
    except ai_provider_guard.ProviderCooldown as e:
        print(f"[info] MiniMax cooldown active, skipping semantic dedup until {e.cooldown_until}")
        conn.close()
        return
    except ProviderAuthenticationError as e:
        print(f"[error] {e}")
        conn.close()
        return
    print(f"[info] {len(all_actions)} raw → {len(deduped)} after dedup (existing: {len(existing)})")

    # Save batch generation log
    save_generation_log({
        "mode": "batch",
        "timestamp": datetime.now().isoformat(),
        "items_count": len(items),
        "batches_count": len(batches),
        "context_loaded": {
            "manifest_length": len(manifest),
            "pulse_fields": {k: len(v) for k, v in pulse_fields.items()},
            "feedback_length": len(feedback_ctx),
            "directions_length": len(directions_text),
            "system_prompt_length": len(system_prompt),
        },
        "raw_actions_count": len(all_actions),
        "deduped_actions_count": len(deduped),
        "actions": [{"title": a.get("title"), "direction": a.get("direction"),
                      "scores": a.get("scores"), "thinking": a.get("thinking")} for a in deduped],
    })

    # Write to DB
    created = 0
    for action in deduped:
        try:
            # Validate required fields
            title = action.get('title', '').strip()
            action_type = action.get('action_type', 'investigate')
            prompt = action.get('prompt', '').strip()
            if not title or not prompt:
                continue
            if action_type not in ('implement', 'investigate', 'content'):
                action_type = 'investigate'
            priority = action.get('priority', 'medium')
            if priority not in ('high', 'medium', 'low', 'bug'):
                priority = 'medium'

            reason = action.get('reason', '')
            if isinstance(reason, (dict, list)):
                reason = json.dumps(reason, ensure_ascii=False)
            src_ids = action.get('source_item_ids', [])
            if isinstance(src_ids, str):
                try: src_ids = json.loads(src_ids)
                except: src_ids = [src_ids]
            if not isinstance(src_ids, list):
                src_ids = []
            # Parse direction from AI response
            act_direction = action.get('direction', '_uncategorized')
            act_direction_label = ''
            if act_direction and directions and act_direction in directions:
                act_direction_label = directions[act_direction].get('label', act_direction)
            elif act_direction == '_uncategorized' or not act_direction:
                act_direction = '_uncategorized'
                act_direction_label = '待归类'
            else:
                # LLM returned unknown slug — treat as uncategorized
                act_direction_label = act_direction
                act_direction = '_uncategorized'

            db.create_action(
                conn,
                source_type='auto',
                title=title,
                action_type=action_type,
                prompt=prompt,
                source_item_ids=src_ids,
                reason=str(reason) if reason else '',
                priority=priority,
                related_project=action.get('related_project'),
                direction=act_direction,
                direction_label=act_direction_label,
            )
            created += 1
        except Exception as e:
            print(f"[warn] Failed to create action '{action.get('title', '')[:40]}': {e}")

    conn.close()
    print(f"[done] Created {created} action points")


if __name__ == '__main__':
    main()
