"""Stage P — 簇内 LLM 清洗（v2 极简版）

设计稿 docs/讨论/clustering/2026-04-29-event-pipeline-v2-design.md §5.5.3 / §5.5.5

策略：每个 dirty 簇并发跑 LLM，识别主事件 + 剔除不属于本簇的 doc。
- LLM = MiniMax M2.7（沿用 v1 enrich_items 同款）
- 输入 = 簇内所有 doc 的 (id, title, ai_summary[:200])
- 事件定义注入 = 按簇 dominant_category 加载 event_definitions/{cat}.md（可插拔）
- 剔除 doc 直接隐藏（cluster_items_v2.removed_at + reason）+ 写日志（cluster_p_log）
- 不兜底 LLM 误判（先观察真实误判率）
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from typing import Iterable

from . import event_definitions

LOGGER = logging.getLogger(__name__)

LLM_MODEL = "MiniMax-M2.7"
LLM_API_BASE = "https://api.minimaxi.com/anthropic/v1"
# v4 优化（2026-04-29）：4096 → 16384。
# Why: v3 实测 MiniMax-M2.7 thinking 模式在 ~3200 token 输入下吃光 4096 max_tokens
# （全部用于 thinking 推理），没空间输出 JSON 答案，cluster 16 / 67 因此 failed。
# 16384 给 thinking (~4-8K) + JSON (~2-3K) 都留充足空间。32K+ 不选：避免超时 + 成本失控。
LLM_MAX_TOKENS = 16384
LLM_TEMPERATURE = 0.2
LLM_TIMEOUT = 180  # 工程优化：从 90s 提到 180s，应对大簇长输入

# 用户决策（2026-04-29）：ai_summary 不做截断，完整传给 LLM。
# Why: 截断会丢掉判断"是不是同事件"的关键证据（具体动作 / 主体 / 时间）。
DOC_SUMMARY_TRUNCATE: int | None = None

# 工程优化：超过该阈值的簇按 fetched_at 排序拆批，每批独立调 LLM 后合并
LARGE_CLUSTER_THRESHOLD = 30
LARGE_CLUSTER_BATCH_SIZE = 25

# 工程优化：批量跑 dirty 簇时的并发度（MiniMax 限流约 ~10 QPM 可承载）
DEFAULT_CONCURRENCY = 4


def _create_ssl_context() -> ssl.SSLContext:
    cafiles = [
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/ssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
    ]
    for cafile in cafiles:
        if cafile and os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


_SSL_CTX = _create_ssl_context()


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_api_key(config: dict | None = None) -> str:
    key = (
        os.environ.get("MINIMAX_API_KEY")
        or (config or {}).get("ai_summary", {}).get("api_key")
        or ""
    )
    if not key:
        raise RuntimeError(
            "MINIMAX_API_KEY 缺失：在 .env 或 config.ai_summary.api_key 中配置"
        )
    return key


def build_system_prompt(event_definition: str | None = None) -> str:
    """拼装 Stage P system prompt（v5 — 中心实体聚合 + 可插拔分类）。

    v5 改动（2026-04-29 用户决策）：
    - 不再按 L1 分文件读 event_definition；直接注入 config/classification.json
      转出来的 14 L1 + L2 完整层级
    - cluster 语义从"事件聚合"变成"中心实体聚合"（围绕一个产品/模型/工具/公司事件的所有内容）
    - 教程 / 评测 / prompt 分享 / 案例 不剔除（围绕中心实体的所有相关内容都保留）
    - 仅剔 4 类：主体跑题 / 同主体不同事件 / 空洞内容 / 重复内容
    - LLM 输出加 cluster_l1（14 L1 之一）+ cluster_l2（subcategories 数组）

    Args:
        event_definition: 已弃用（v4 兼容参数）。v5 直接读 classification.json。
    """
    classification_block = event_definitions.load_classification_block()
    return f"""# 角色
你是 info2action 系统的内容簇质量审核员。

# 产品背景
info2action 是一个跨平台 AI / 科技 / 投资 / 创业 / 创作信息聚合工具。它从 Twitter、Reddit、
HackerNews、RSS、B 站、公众号、lingowhale 等多个平台抓取最新内容，再把围绕**同一中心实体**
（具体产品 / 模型版本 / 工具 / 公司事件 / 比赛活动）的报道合并成一个卡片簇展示给用户。

产品价值：用户用一张卡片能一眼看清"GPT-5.5"这个产品的全貌——
官方公告 + 推特首发反应 + HN 讨论 + 中文媒体解读 + prompt 教程 + 用法案例 + 评测对比，
不需要在 7 个平台之间来回切。

# info2action 的分类体系（v4.1，14 个 L1）

下面是分类全文。Stage P 输出时必须把簇的 cluster_l1 / cluster_l2 标到这个体系里。

{classification_block}

# 你的任务（"中心实体聚合"）

上游聚簇算法（cosine 相似度）会把语义相近的 doc 合到一起，但难免混入"主题相邻但中心实体不同"的 doc。
你的任务是判断簇的中心实体，剔除那些不围绕该中心实体的 doc，并把簇分到合适的 L1 / L2。

## 中心实体的颗粒度

中心实体是一个**具体的**产品 / 模型版本 / 工具 / 公司具体动作 / 比赛活动 / 创作主题。
不是泛话题（"AI 行业动态" / "Claude Code 相关讨论" / "AI 工具推荐合集"等都不是合格的中心实体）。

✅ 合格中心实体例子：
- 产品："GPT Image 2 发布" / "ChatGPT Atlas 浏览器发布"
- 模型："DeepSeek V4 发布" / "Qwen3.6-27B 发布"
- 工具："Cursor 1.0 GA" / "Claude Code SDK 修复 bug"
- 公司事件："Anthropic C 轮融资 60 亿" / "美国限制 H200 出口"
- 教程主题："GPT-Image-2 50+ Case 实测"（明确围绕一个工具的教程合集）
- 比赛："WaytoAGI 黑客松"

❌ 不合格中心实体（簇 certainty=low + 大量 removed）：
- "AI 工具推荐合集" / "今日 AI 动态" / "Claude Code 技巧汇总"
- "2025 AI 行业反思" / "Agent 生态多元化"

# 哪些 doc 应保留（围绕中心实体）

✅ 哪怕是教程 / Cookbook / 评测 / prompt 分享 / 用法案例 / 创意演示，**只要中心实体一致就保留**。
   例：簇是"GPT-5.5 发布"，则"GPT-5.5 prompt 技巧" / "GPT-5.5 vs Claude Opus 对比" /
   "GPT-5.5 编程实测" 都应保留，因为它们都围绕"GPT-5.5"这个中心实体。

# 哪些 doc 应剔除（仅 4 类）

1. **主体完全跑题**：簇是"GPT-5.5"，混进了比特币行情 / 体育新闻
2. **同主体但不同事件**：簇是"GPT Image 2 发布"，混进了"GPT-5.5 发布"——同公司但**不同产品**就是不同中心实体（用户决策：肯定剔除）
3. **空洞内容**：纯 emoji / 标题党无信息 / 营销引流帖 / 仅含标签和链接 / 仅一句调侃
4. **重复内容**：同作者同内容发了两遍

**除以上 4 类外，一律保留**。哪怕看起来"主题相邻"或"质量一般"，只要中心实体一致就保留。

# 你的工作步骤

1. 通读这批 doc，识别它们共同指向的**中心实体**（具体产品/模型/工具/公司事件/比赛/创作主题）
2. 按 4 类剔除规则把不属于本中心实体的 doc 剔除
3. 把簇分到 v4.1 分类体系的某个 L1（必须是上面 14 个 L1 之一，不能编造）+ 一个或多个 L2（必须隶属已选 L1）
4. 不确定时倾向保守：宁愿少剔除也不要错杀（漏剔 OK，错剔不行）；如果簇内多数 doc 围绕的中心实体不清晰，整簇 event_certainty=low 并多剔
""".strip()


def build_user_prompt(items: list[dict]) -> str:
    """拼装 Stage P user prompt。每条 doc 一行：[id=xxx] 标题 | 摘要前 200 字。"""
    lines: list[str] = [f"以下是 {len(items)} 个被聚类算法合并到同一簇的内容卡片："]
    lines.append("")
    for it in items:
        title = (it.get("title") or "").strip().replace("\n", " ")
        summary = (it.get("ai_summary") or "").strip().replace("\n", " ")
        if DOC_SUMMARY_TRUNCATE is not None and len(summary) > DOC_SUMMARY_TRUNCATE:
            summary = summary[:DOC_SUMMARY_TRUNCATE] + "…"
        lines.append(f"[id={it['id']}] {title} | {summary}")
    lines.extend([
        "",
        "请按以下 JSON 格式输出（不要任何额外说明文字，不要 markdown 代码块）：",
        "",
        "{",
        '  "cluster_l1": "products" | "efficiency_tools" | "coding" | "skill" | "models" | "eval" | "tech" | "tutorials" | "industry" | "creator" | "investment" | "startup" | "events" | "other",',
        '  "cluster_l2": ["..."],   // 数组，至少 1 个，必须隶属已选 L1（参考分类体系里的 L2 列表）',
        '  "event_summary": "用一句中文描述这批卡片围绕的中心实体（≤80 字）",',
        '  "event_certainty": "high" | "medium" | "low",',
        '  "removed": [',
        '    {"id": "xxx", "reason": "为什么这条不围绕本簇中心实体（≤40 字，必须具体；只剔 4 类：跑题/不同事件/空洞/重复）"}',
        "  ],",
        '  "kept_ids": ["yyy", "zzz", ...]',
        "}",
        "",
        "# 硬性要求",
        "- cluster_l1 必须是 14 个 L1 之一（products / efficiency_tools / coding / skill / models / eval / tech / tutorials / industry / creator / investment / startup / events / other）",
        "- cluster_l2 至少 1 个，且每个 L2 id 必须隶属 cluster_l1（参考 system 里的分类体系）",
        "- event_certainty=low 时倾向保守：宁愿少剔除也不要错杀",
        "- removed + kept_ids 的总数必须严格等于输入卡片总数",
        "- 每个 id 只能出现在 removed 或 kept_ids 之一,绝对不能重复",
        "- removed[*].id 之间不能有重复;kept_ids 内部也不能有重复",
        "- 不能出现 removed 和 kept_ids 之间互相重复的 id",
        "- 仅以下 4 类应剔除（其他一律保留，哪怕是教程 / 评测 / prompt 分享 / 用法案例）：",
        "  1) 主体完全跑题（如 GPT-5.5 簇混进比特币）",
        "  2) 同主体但不同事件（如 GPT Image 2 簇混进 GPT-5.5 — 不同产品就是不同中心实体）",
        "  3) 空洞内容（纯 emoji / 标题党 / 营销引流 / 仅链接 / 仅一句调侃）",
        "  4) 重复内容（同作者同内容发了两遍）",
        '- reason 必须是具体内容判断（错例："内容相关性低"；正例："这条是 GPT-5.5 发布报道，不属于 GPT Image 2 中心实体"）',
        "- 输出必须是合法 JSON，无 markdown 包裹",
        "- 输出之前你应自己核对一遍：所有输入 id 是否恰好出现一次（在 removed 或 kept_ids），cluster_l2 是否都隶属 cluster_l1，如果不满足就重新组织输出",
    ])
    return "\n".join(lines)


def _call_minimax(api_key: str, system_prompt: str, user_content: str) -> str:
    url = f"{LLM_API_BASE}/messages"
    payload = json.dumps({
        "model": LLM_MODEL,
        "system": system_prompt,
        "max_tokens": LLM_MAX_TOKENS,
        "temperature": LLM_TEMPERATURE,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT, context=_SSL_CTX) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"].strip()
    raise RuntimeError(f"MiniMax 响应缺少 text content: {result!r}")


def _strip_json_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        # 容错：去掉 markdown code fence
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _bounded_edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Return Levenshtein distance, capped above ``limit``.

    MiniMax occasionally copies long item ids with one wrong digit or a tiny
    prefix typo. We only use this for conservative, one-to-one repair when the
    LLM output is otherwise structurally valid.
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
            row_min = min(row_min, cur[-1])
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _repair_close_id_typos(
    removed: list[dict],
    kept_ids: list[str],
    expected_ids: set[str],
) -> tuple[list[dict], list[str]]:
    """Conservatively repair unambiguous near-miss item ids from LLM output."""
    observed = {str(e["id"]).strip() for e in removed} | set(kept_ids)
    missing = set(expected_ids - observed)
    extra = sorted(observed - expected_ids)
    if not missing or not extra:
        return removed, kept_ids

    mapping: dict[str, str] = {}
    remaining_missing = set(missing)
    for extra_id in extra:
        candidates: list[tuple[int, float, str]] = []
        for expected in remaining_missing:
            distance = _bounded_edit_distance(extra_id, expected, limit=3)
            similarity = SequenceMatcher(None, extra_id, expected).ratio()
            if distance <= 3 and similarity >= 0.90:
                candidates.append((distance, -similarity, expected))
        candidates.sort()
        if len(candidates) != 1:
            return removed, kept_ids
        _, _, replacement = candidates[0]
        mapping[extra_id] = replacement
        remaining_missing.remove(replacement)

    if remaining_missing:
        return removed, kept_ids

    repaired_removed = [
        {**entry, "id": mapping.get(str(entry["id"]).strip(), str(entry["id"]).strip())}
        for entry in removed
    ]
    repaired_kept = [mapping.get(kid, kid) for kid in kept_ids]
    return repaired_removed, repaired_kept


def parse_response(raw: str, expected_ids: set[str]) -> dict:
    """严格校验 LLM 输出。失败抛 ValueError。

    v5 加 cluster_l1 / cluster_l2 校验。

    要求：
    - 合法 JSON
    - cluster_l1 / cluster_l2 / event_summary / event_certainty / removed / kept_ids 全部存在
    - cluster_l1 ∈ 14 个 L1 白名单
    - cluster_l2 是非空数组，每个值必须隶属 cluster_l1（按 classification.json 的 subcategories）
    - removed[*]['id'] + kept_ids 集合 = expected_ids（无遗漏、无新增、无重复）
    - event_certainty ∈ {high, medium, low}
    """
    cleaned = _strip_json_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"输出不是合法 JSON: {e}; raw[:200]={raw[:200]!r}") from e
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是 object: {type(data).__name__}")
    for k in ("cluster_l1", "cluster_l2", "event_summary", "event_certainty", "removed", "kept_ids"):
        if k not in data:
            raise ValueError(f"缺字段 {k}")
    cert = str(data["event_certainty"]).strip().lower()
    if cert not in ("high", "medium", "low"):
        raise ValueError(f"event_certainty 非法: {cert!r}")
    data["event_certainty"] = cert

    # cluster_l1 校验
    valid_l1 = set(event_definitions.load_l1_ids())
    cluster_l1 = str(data["cluster_l1"]).strip().lower()
    if cluster_l1 not in valid_l1:
        raise ValueError(f"cluster_l1 非法: {cluster_l1!r}; 合法 L1: {sorted(valid_l1)}")
    data["cluster_l1"] = cluster_l1

    # cluster_l2 校验
    cluster_l2_raw = data["cluster_l2"]
    if cluster_l1 == "other":
        # The taxonomy's `other` bucket has no subcategories. The prompt still
        # asks for at least one L2, so normalize common model output
        # ["other"] back to an empty L2 list.
        data["cluster_l2"] = []
        cluster_l2: list[str] = []
    elif not isinstance(cluster_l2_raw, list) or not cluster_l2_raw:
        raise ValueError(f"cluster_l2 必须是非空数组: {cluster_l2_raw!r}")
    else:
        l1_l2_map = event_definitions.load_l1_l2_map()
        valid_l2_for_l1 = l1_l2_map.get(cluster_l1, set())
        cluster_l2 = []
        for l2 in cluster_l2_raw:
            l2_clean = str(l2).strip().lower()
            if l2_clean not in valid_l2_for_l1:
                if "other" in valid_l2_for_l1:
                    l2_clean = "other"
                else:
                    raise ValueError(
                        f"cluster_l2={l2_clean!r} 不隶属 cluster_l1={cluster_l1!r}；"
                        f"合法 L2: {sorted(valid_l2_for_l1)}"
                    )
            if l2_clean not in cluster_l2:
                cluster_l2.append(l2_clean)
        data["cluster_l2"] = cluster_l2

    removed = data["removed"] or []
    if not isinstance(removed, list):
        raise ValueError("removed 必须是 list")
    removed_entries: list[dict] = []
    removed_ids: set[str] = set()
    for entry in removed:
        if not isinstance(entry, dict):
            raise ValueError(f"removed 元素必须是 object: {entry!r}")
        if "id" not in entry or "reason" not in entry:
            raise ValueError(f"removed 元素缺 id/reason: {entry!r}")
        rid = str(entry["id"]).strip()
        if rid in removed_ids:
            raise ValueError(f"removed 重复 id: {rid}")
        removed_ids.add(rid)
        removed_entries.append({"id": rid, "reason": str(entry["reason"]).strip()})

    kept_ids_raw = data["kept_ids"] or []
    if not isinstance(kept_ids_raw, list):
        raise ValueError("kept_ids 必须是 list")
    kept_ids: list[str] = []
    seen_kept: set[str] = set()
    for kid in kept_ids_raw:
        kid_s = str(kid).strip()
        if kid_s in seen_kept:
            raise ValueError(f"kept_ids 重复: {kid_s}")
        seen_kept.add(kid_s)
        kept_ids.append(kid_s)

    overlap = removed_ids & seen_kept
    if overlap:
        # The model sometimes lists an id in removed with a concrete reason,
        # then accidentally includes it again in kept_ids. Prefer the explicit
        # removal decision; the final closure check below still guards against
        # missing/extra ids.
        kept_ids = [kid for kid in kept_ids if kid not in overlap]
        seen_kept = set(kept_ids)

    removed_entries, kept_ids = _repair_close_id_typos(
        removed_entries, kept_ids, expected_ids
    )
    removed_ids = {entry["id"] for entry in removed_entries}
    seen_kept = set(kept_ids)
    overlap = removed_ids & seen_kept
    if overlap:
        raise ValueError(f"id 同时出现在 removed 和 kept_ids: {sorted(overlap)}")
    union = removed_ids | seen_kept
    missing = expected_ids - union
    extra = union - expected_ids
    if missing and not extra:
        # Conservative fallback: if the model simply omitted some input ids,
        # keep them. This follows the Stage P policy of preferring under-removal
        # over accidental removal.
        kept_ids.extend(sorted(missing))
        seen_kept = set(kept_ids)
        union = removed_ids | seen_kept
        missing = expected_ids - union
        extra = union - expected_ids
    if missing or extra:
        raise ValueError(f"id 集合不闭合 missing={sorted(missing)} extra={sorted(extra)}")

    return {
        "cluster_l1": cluster_l1,
        "cluster_l2": cluster_l2,
        "event_summary": str(data["event_summary"]).strip(),
        "event_certainty": cert,
        "removed": removed_entries,
        "kept_ids": kept_ids,
    }


def _llm_decide_with_batching(api_key: str, system_prompt: str, members: list[dict],
                               cluster_id: int) -> tuple[dict | None, Exception | None, str]:
    """大簇拆批 LLM 决策。

    按 fetched_at 排序后拆成 ≤LARGE_CLUSTER_BATCH_SIZE 的子批，每批独立调 LLM
    判 keep/remove，最后合并：
    - 任一子批失败 → 整簇判 failed（保守，不留半结果）
    - event_summary 取第一个子批的输出（通常包含最新的 doc，主事件信号最强）
    - event_certainty 取最低（high > medium > low），保守
    - removed / kept 按子批合并

    Returns: (parsed | None, last_err | None, last_raw_response)
    """
    sorted_members = sorted(
        members, key=lambda m: m.get("fetched_at") or "", reverse=True
    ) if any(m.get("fetched_at") for m in members) else list(members)

    batches = [sorted_members[i:i + LARGE_CLUSTER_BATCH_SIZE]
               for i in range(0, len(sorted_members), LARGE_CLUSTER_BATCH_SIZE)]

    LOGGER.info("Stage P cluster=%d 大簇拆批: %d 成员 → %d 批",
                cluster_id, len(members), len(batches))

    merged_removed: list[dict] = []
    merged_kept_ids: list[str] = []
    summaries: list[str] = []
    certainties: list[str] = []
    cluster_l1s: list[str] = []
    cluster_l2s: list[list[str]] = []
    last_raw = ""
    last_err: Exception | None = None

    cert_rank = {"high": 0, "medium": 1, "low": 2}

    for bi, batch in enumerate(batches, 1):
        batch_ids = {m["id"] for m in batch}
        batch_user = build_user_prompt(batch)
        batch_parsed: dict | None = None
        for attempt in range(1, 7):  # 6 次 retry（429 指数退避用）
            try:
                last_raw = _call_minimax(api_key, system_prompt, batch_user)
                batch_parsed = parse_response(last_raw, batch_ids)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < 6:
                    backoff = min(60.0, 2.0 * (2 ** (attempt - 1))) + random.random() * 2
                    LOGGER.warning("Stage P cluster=%d batch=%d 撞 429，退避 %.1fs (attempt %d)",
                                   cluster_id, bi, backoff, attempt)
                    time.sleep(backoff)
                    continue
                LOGGER.warning("Stage P cluster=%d batch=%d HTTP %d 第 %d 次失败: %s",
                               cluster_id, bi, e.code, attempt, e)
            except Exception as e:  # noqa: BLE001
                last_err = e
                LOGGER.warning("Stage P cluster=%d batch=%d 第 %d 次失败: %s",
                               cluster_id, bi, attempt, e)
        if batch_parsed is None:
            return None, last_err, last_raw
        merged_removed.extend(batch_parsed["removed"])
        merged_kept_ids.extend(batch_parsed["kept_ids"])
        summaries.append(batch_parsed["event_summary"])
        certainties.append(batch_parsed["event_certainty"])
        cluster_l1s.append(batch_parsed["cluster_l1"])
        cluster_l2s.append(batch_parsed["cluster_l2"])

    # 选第一个子批的 event_summary（按 fetched_at DESC，主事件信号最强）
    final_summary = summaries[0] if summaries else ""
    # certainty 取最低（最保守的）
    final_cert = max(certainties, key=lambda c: cert_rank.get(c, 9)) if certainties else "low"
    # cluster_l1 取多数（少数子批可能误判）
    from collections import Counter
    final_l1 = Counter(cluster_l1s).most_common(1)[0][0] if cluster_l1s else "other"
    # cluster_l2 取所有子批中 cluster_l1 == final_l1 的 union
    final_l2: list[str] = []
    for l1, l2_list in zip(cluster_l1s, cluster_l2s):
        if l1 != final_l1:
            continue
        for l2 in l2_list:
            if l2 not in final_l2:
                final_l2.append(l2)
    if not final_l2:
        # 兜底：任一子批的 L2，按隶属规则不一定合，但避免空数组
        for l2_list in cluster_l2s:
            for l2 in l2_list:
                if l2 not in final_l2:
                    final_l2.append(l2)
                    break
            if final_l2:
                break

    parsed = {
        "cluster_l1": final_l1,
        "cluster_l2": final_l2,
        "event_summary": final_summary,
        "event_certainty": final_cert,
        "removed": merged_removed,
        "kept_ids": merged_kept_ids,
    }
    return parsed, None, last_raw


def fetch_dirty_cluster_dump(conn, cluster_id: int) -> tuple[str | None, list[dict]]:
    """读取一个 cluster 待清洗的成员（仅 removed_at IS NULL 的）。"""
    cluster_row = conn.execute(
        "SELECT id, dominant_category FROM clusters_v2 WHERE id = ?",
        (cluster_id,),
    ).fetchone()
    if not cluster_row:
        return None, []
    member_rows = conn.execute(
        """SELECT i.id, i.title, i.ai_summary, i.ai_category, i.fetched_at
           FROM cluster_items_v2 ci
           JOIN items i ON i.id = ci.item_id
           WHERE ci.cluster_id = ? AND ci.removed_at IS NULL
           ORDER BY ci.added_at""",
        (cluster_id,),
    ).fetchall()
    return cluster_row["dominant_category"], [dict(r) for r in member_rows]


def run_stage_p_for_cluster(conn, cluster_id: int, *, api_key: str | None = None,
                             config: dict | None = None) -> dict:
    """对单个 cluster 跑 Stage P。

    Returns:
        {cluster_id, status, kept, removed, certainty, took_seconds}
        status ∈ {clean, skipped, failed}
    """
    import time
    started = time.time()
    dominant, members = fetch_dirty_cluster_dump(conn, cluster_id)
    if not members:
        return {"cluster_id": cluster_id, "status": "skipped",
                "reason": "no visible members", "took_seconds": 0.0}

    # v5: 不再按 dominant 过滤，所有 cluster 都走 LLM。LLM 自己判 cluster_l1（含 other）。

    # 单成员簇没有"剔除"的意义，直接标 clean + 不调 LLM
    if len(members) == 1:
        conn.execute(
            """UPDATE clusters_v2 SET stage_p_state='clean',
                                       stage_p_run_at=?,
                                       event_summary=?,
                                       event_certainty='medium'
               WHERE id=?""",
            (_utc_now_iso(),
             (members[0].get("title") or "").strip()[:80],
             cluster_id),
        )
        conn.commit()
        return {"cluster_id": cluster_id, "status": "clean",
                "kept": 1, "removed": 0, "certainty": "medium",
                "took_seconds": round(time.time() - started, 2)}

    api_key = api_key or _resolve_api_key(config)
    system_prompt = build_system_prompt()

    # 工程优化：大簇拆批 — 超过 LARGE_CLUSTER_THRESHOLD 按 fetched_at 排序拆 ≤25/批
    if len(members) > LARGE_CLUSTER_THRESHOLD:
        parsed, last_err, raw_response = _llm_decide_with_batching(
            api_key, system_prompt, members, cluster_id
        )
    else:
        last_err = None
        parsed = None
        raw_response = ""
        expected_ids = {m["id"] for m in members}
        for attempt in range(1, 4):
            try:
                user_prompt = build_user_prompt(members)
                raw_response = _call_minimax(api_key, system_prompt, user_prompt)
                parsed = parse_response(raw_response, expected_ids)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429 and attempt < 3:
                    backoff = min(45.0, 2.0 * (2 ** (attempt - 1))) + random.random() * 2
                    LOGGER.warning("Stage P cluster=%d 撞 429，退避 %.1fs (attempt %d)",
                                   cluster_id, backoff, attempt)
                    time.sleep(backoff)
                    continue
                LOGGER.warning("Stage P cluster=%d HTTP %d 第 %d 次失败: %s",
                               cluster_id, e.code, attempt, e)
            except Exception as e:  # noqa: BLE001
                last_err = e
                LOGGER.warning("Stage P cluster=%d 第 %d 次失败: %s", cluster_id, attempt, e)

    if parsed is None or last_err is not None:
        conn.execute(
            """UPDATE clusters_v2 SET stage_p_state='failed',
                                       stage_p_run_at=?,
                                       stage_p_failed_reason=?
               WHERE id=?""",
            (_utc_now_iso(), str(last_err)[:500], cluster_id),
        )
        conn.execute(
            """INSERT INTO cluster_p_log
                 (cluster_id, action, reason, llm_model, raw_response)
               VALUES (?, 'failed', ?, ?, ?)""",
            (cluster_id, str(last_err)[:500], LLM_MODEL, raw_response[:8000]),
        )
        conn.commit()
        return {"cluster_id": cluster_id, "status": "failed",
                "reason": str(last_err),
                "took_seconds": round(time.time() - started, 2)}

    now = _utc_now_iso()
    # v5: cluster_l1 → dominant_category；cluster_l2 拼成 JSON 也存进同字段以方便 inspect
    # （schema 没改，把 L1 + L2 合并成 "L1[/L2,L2]" 形态）
    cluster_l1 = parsed.get("cluster_l1") or "other"
    cluster_l2 = parsed.get("cluster_l2") or []
    dominant_field = f"{cluster_l1}[/{','.join(cluster_l2)}]" if cluster_l2 else cluster_l1
    conn.execute(
        """UPDATE clusters_v2 SET stage_p_state='clean',
                                   stage_p_run_at=?,
                                   event_summary=?,
                                   event_certainty=?,
                                   dominant_category=?
           WHERE id=?""",
        (now, parsed["event_summary"][:500], parsed["event_certainty"],
         dominant_field, cluster_id),
    )
    for entry in parsed["removed"]:
        conn.execute(
            """UPDATE cluster_items_v2 SET removed_at=?, removed_reason=?
               WHERE cluster_id=? AND item_id=?""",
            (now, entry["reason"][:500], cluster_id, entry["id"]),
        )
        conn.execute(
            """INSERT INTO cluster_p_log
                 (cluster_id, item_id, action, reason, llm_model, raw_response)
               VALUES (?, ?, 'remove', ?, ?, ?)""",
            (cluster_id, entry["id"], entry["reason"][:500],
             LLM_MODEL, raw_response[:8000] if entry == parsed["removed"][0] else None),
        )
    # 总日志（kept summary）
    conn.execute(
        """INSERT INTO cluster_p_log
             (cluster_id, item_id, action, reason, llm_model)
           VALUES (?, NULL, 'summary', ?, ?)""",
        (cluster_id,
         json.dumps({
             "cluster_l1": cluster_l1,
             "cluster_l2": cluster_l2,
             "event_summary": parsed["event_summary"],
             "event_certainty": parsed["event_certainty"],
             "kept_n": len(parsed["kept_ids"]),
             "removed_n": len(parsed["removed"]),
         }, ensure_ascii=False),
         LLM_MODEL),
    )
    # 同步 member_count（visible 成员数）
    visible = conn.execute(
        "SELECT COUNT(*) AS n FROM cluster_items_v2 WHERE cluster_id=? AND removed_at IS NULL",
        (cluster_id,),
    ).fetchone()
    conn.execute(
        "UPDATE clusters_v2 SET member_count=? WHERE id=?",
        (visible["n"], cluster_id),
    )
    conn.commit()

    return {
        "cluster_id": cluster_id,
        "status": "clean",
        "kept": len(parsed["kept_ids"]),
        "removed": len(parsed["removed"]),
        "certainty": parsed["event_certainty"],
        "cluster_l1": cluster_l1,
        "cluster_l2": cluster_l2,
        "took_seconds": round(time.time() - started, 2),
    }


def query_dirty_cluster_ids(conn, *, limit: int | None = None) -> list[int]:
    sql = (
        "SELECT id FROM clusters_v2 "
        "WHERE stage_p_state='dirty' "
        "ORDER BY created_at DESC"
    )
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return [r["id"] for r in conn.execute(sql, params).fetchall()]


def run_pending_stage_p(conn, *, limit: int | None = None,
                         api_key: str | None = None,
                         config: dict | None = None) -> list[dict]:
    """扫所有 dirty 簇跑 Stage P，串行执行（"每出一簇立即跑"语义在 Z+P 流水线中实现）。"""
    cluster_ids = query_dirty_cluster_ids(conn, limit=limit)
    return [run_stage_p_for_cluster(conn, cid, api_key=api_key, config=config)
            for cid in cluster_ids]


def run_pending_stage_p_concurrent(db_path: str, cluster_ids: list[int], *,
                                     concurrency: int = DEFAULT_CONCURRENCY,
                                     api_key: str | None = None,
                                     config: dict | None = None) -> list[dict]:
    """并发版本：每个 worker 自己开 sqlite 连接，避免 conn 跨线程共享。

    Args:
        db_path: SQLite 文件绝对路径
        cluster_ids: 要跑的 dirty cluster_id 列表
        concurrency: 并发度（默认 4）

    Why 不复用 run_pending_stage_p：
      sqlite3.Connection 不是 thread-safe，并发多 worker 必须每个 worker 独立 connect。
    """
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor, as_completed

    api_key_resolved = api_key or _resolve_api_key(config)

    def worker(cid: int) -> dict:
        conn = sqlite3.connect(db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            return run_stage_p_for_cluster(conn, cid, api_key=api_key_resolved, config=config)
        finally:
            conn.close()

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_to_cid = {ex.submit(worker, cid): cid for cid in cluster_ids}
        for fut in as_completed(future_to_cid):
            cid = future_to_cid[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                LOGGER.error("Stage P cluster=%d worker 异常: %s", cid, e)
                results.append({"cluster_id": cid, "status": "failed", "reason": str(e)})
    return results
