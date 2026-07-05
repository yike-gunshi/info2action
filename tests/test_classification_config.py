"""Regression tests for the homepage classification taxonomy.

Updated 2026-05-24 for v4.1 重构:
- L1 从 13 → 14(新增 eval；此前新增 efficiency_tools / coding / skill / startup / events,
  ai_tools 改名 efficiency_tools)
- 每个 L1 含 subcategories 结构化对象(L2)
- 每个 L1 必须有 'other' L2 兜底(other L1 除外)
"""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _data() -> dict:
    return json.loads((ROOT / "config" / "classification.json").read_text())


def _category(cat_id: str) -> dict:
    return next(c for c in _data()["categories"] if c["id"] == cat_id)


def test_v4_1_l1_order_and_count():
    data = _data()
    assert data["version"] == "4.1"
    assert [c["id"] for c in data["categories"]] == [
        "products",
        "efficiency_tools",
        "coding",
        "skill",
        "models",
        "eval",
        "tech",
        "tutorials",
        "industry",
        "creator",
        "investment",
        "startup",
        "events",
        "other",
    ]
    # L1 display names (改名后 — 短名称)
    assert _category("products")["name"] == "产品"
    assert _category("efficiency_tools")["name"] == "工具"
    assert _category("coding")["name"] == "Coding"
    assert _category("skill")["name"] == "Skill"
    assert _category("eval")["name"] == "评测"
    assert _category("events")["name"] == "活动"


def test_each_l1_has_other_subcategory():
    """v4.0 设计决策: 每个 visible L1 必须有 'other' L2 兜底。"""
    data = _data()
    for cat in data["categories"]:
        if cat["id"] == "other":
            continue  # other L1 自身不分 L2
        sub_ids = [s["id"] for s in cat.get("subcategories", [])]
        assert "other" in sub_ids, f"L1 '{cat['id']}' 缺 'other' L2 兜底"


def test_subcategories_are_structured_objects():
    """v4.0 schema: subcategories[] 元素必须是 {id, name, examples?} 对象。"""
    data = _data()
    for cat in data["categories"]:
        for sub in cat.get("subcategories", []):
            assert isinstance(sub, dict), f"{cat['id']}.subcategories 元素必须是 dict"
            assert "id" in sub and "name" in sub, f"{cat['id']}.{sub} 缺 id/name"


def test_coding_l2_covers_user_decisions():
    """user Q14 锁定 coding 优先,L2 含 工具 / Agent 框架 / 设计辅助 / 开发方法论"""
    coding = _category("coding")
    sub_ids = {s["id"] for s in coding["subcategories"]}
    assert {"coding_tool", "agent_framework", "design_aid", "dev_method"}.issubset(sub_ids)
    # coding boundary_rule 必须强调"优先于 AI 产品"
    assert "Coding 优先" in coding["boundary_rule"]


def test_models_split_by_modality():
    """user Q4 锁定 models 按模态拆 L2"""
    models = _category("models")
    sub_ids = {s["id"] for s in models["subcategories"]}
    # 至少覆盖 LLM / 图像 / 视频 / 语音 / 世界模型 / other
    assert {"llm", "image_model", "video_model", "audio_model", "world_model", "other"}.issubset(sub_ids)


def test_eval_l2_covers_user_decisions():
    """user 2026-05-24: eval 覆盖对象维度 + 评测价值形态。"""
    eval_cat = _category("eval")
    sub_ids = {s["id"] for s in eval_cat["subcategories"]}
    assert {
        "model_eval",
        "product_eval",
        "coding_eval",
        "agent_eval",
        "safety_eval",
        "eval_benchmarks",
        "eval_methods",
        "eval_practice",
        "eval_reliability",
        "other",
    }.issubset(sub_ids)
    assert "AI 领域评测知识资产" in eval_cat["boundary_rule"]
    assert "普通产品体验" in eval_cat["boundary_rule"]


def test_tech_no_multi_agent_after_merge():
    """user 2026-04-29: multi_agent 合并到 agent,不再单独存在。"""
    tech = _category("tech")
    sub_ids = {s["id"] for s in tech["subcategories"]}
    assert "multi_agent" not in sub_ids
    assert "agent" in sub_ids


def test_skill_no_startup_discuss_after_user_removal():
    """user 2026-04-29: skill 删除 startup_discuss(跟 startup L1 重叠)。"""
    skill = _category("skill")
    sub_ids = {s["id"] for s in skill["subcategories"]}
    assert "startup_discuss" not in sub_ids


def test_other_l1_visible_false_by_default():
    """other L1 默认隐藏(过滤层兜底,visible=false 内容也走这里)。"""
    other = _category("other")
    assert other["visible"] is False
    # 边界规则要明确两类用法(具体 vs 完全跨主题)
    text = other["boundary_rule"]
    assert "其他" in text or "other" in text.lower()


def test_v4_changelog_present():
    """changelog 必须有 v4.0 条目。"""
    data = _data()
    changes = data.get("changelog", [])
    assert any("v4.0" in str(c.get("changes", "")) for c in changes)
    assert any("v4.1" in str(c.get("changes", "")) for c in changes)
