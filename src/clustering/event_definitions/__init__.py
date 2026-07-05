"""可插拔分类注入。

v5 改动（2026-04-29）：
  - 不再按 L1 分文件保存 event_definition；删除 products/models/industry/ai_tools.md
  - 直接读 config/classification.json，复用 enrich_items.build_category_block 转 markdown
  - 单一来源：classification.json 改 → enrichment + Stage P 同步生效

设计稿: docs/讨论/clustering/2026-04-29-classification-v4-discussion.md
"""
from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLASSIFICATION_PATH = _REPO_ROOT / "config" / "classification.json"


def _build_category_block(categories: list[dict]) -> str:
    """复刻 src/enrich_items.py 的 build_category_block 输出格式。

    保持与 enrichment prompt 同款，单一分类语义来源。
    """
    lines: list[str] = []
    for cat in categories:
        cid = cat.get("id", "")
        name = cat.get("name", "")
        desc = cat.get("description", "")
        rule = cat.get("boundary_rule", "")
        subs = cat.get("subcategories") or []
        lines.append(f"## L1: {cid}({name})")
        if desc:
            lines.append(f"定位: {desc}")
        if rule:
            lines.append(f"边界规则: {rule}")
        if subs:
            lines.append("L2:")
            for sub in subs:
                sid = sub.get("id", "")
                sname = sub.get("name", "")
                examples = sub.get("examples") or []
                ex_text = f" 例: {', '.join(examples[:6])}" if examples else ""
                lines.append(f"  - {sid}({sname}){ex_text}")
        lines.append("")
    return "\n".join(lines)


def load_classification_block() -> str:
    """读 config/classification.json 转成可注入 prompt 的 L1+L2 markdown 块。"""
    if not _CLASSIFICATION_PATH.exists():
        raise RuntimeError(f"classification.json 不存在：{_CLASSIFICATION_PATH}")
    with _CLASSIFICATION_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return _build_category_block(data.get("categories") or [])


def load_l1_ids() -> list[str]:
    """返回所有有效 L1 id（用于 parse_response 校验）。"""
    with _CLASSIFICATION_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [str(cat.get("id", "")) for cat in (data.get("categories") or []) if cat.get("id")]


def load_l1_l2_map() -> dict[str, set[str]]:
    """返回 {L1 id: {L2 id}} 映射（用于 parse_response L2 隶属校验）。"""
    with _CLASSIFICATION_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        cat.get("id", ""): {sub.get("id", "") for sub in (cat.get("subcategories") or []) if sub.get("id")}
        for cat in (data.get("categories") or [])
    }
