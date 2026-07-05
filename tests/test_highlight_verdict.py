from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import highlight_verdict  # noqa: E402


def _raw(**overrides):
    base = {
        "reason": "①实质收获：给出可照做方法",
        "verdict": "featured",
        "value_path": "substantive",
        "uncertainty": "none",
        "confidence": 0.82,
        "scores": {
            "importance": 2,
            "novelty": 2,
            "credibility": 3,
            "substance": 3,
            "actionability": 3,
        },
        "ai_relevant": "yes",
        "spam": 1,
    }
    base.update(overrides)
    return base


def test_featured_verdict_is_included():
    result = highlight_verdict.normalize_verdict_result(_raw())

    assert result["highlight_include_in_highlights"] is True
    assert result["cluster_verdict"] == "featured"


def test_positive_borderline_substantive_is_included():
    result = highlight_verdict.normalize_verdict_result(
        _raw(
            verdict="borderline",
            value_path="substantive",
            uncertainty="thin_detail",
            reason="①实质收获：标题像教程，但摘要细节偏薄",
        )
    )

    assert result["highlight_include_in_highlights"] is True
    assert result["cluster_verdict"] == "positive_borderline"


def test_positive_borderline_lead_value_is_included():
    result = highlight_verdict.normalize_verdict_result(
        _raw(
            verdict="borderline",
            value_path="lead_value",
            uncertainty="needs_source",
            reason="③线索价值：具体资源值得跟进，但来源信息偏少",
        )
    )

    assert result["highlight_include_in_highlights"] is True
    assert result["cluster_verdict"] == "positive_borderline"


def test_unverified_major_claim_borderline_is_excluded():
    result = highlight_verdict.normalize_verdict_result(
        _raw(
            verdict="borderline",
            value_path="major_event",
            uncertainty="unverified_major_claim",
            reason="②重要事件：重大声称缺少可验证来源",
        )
    )

    assert result["highlight_include_in_highlights"] is False
    assert result["cluster_verdict"] == "risk_borderline"


def test_drop_is_excluded():
    result = highlight_verdict.normalize_verdict_result(
        _raw(
            verdict="drop",
            value_path="none",
            uncertainty="none",
            reason="相关性闸：与 AI 受众无直接关系",
        )
    )

    assert result["highlight_include_in_highlights"] is False
    assert result["cluster_verdict"] == "drop"


def test_invalid_or_missing_required_fields_become_pending():
    result = highlight_verdict.normalize_verdict_result('{"verdict":"featured"}')

    assert result["highlight_include_in_highlights"] is False
    assert result["cluster_verdict"] == "pending"
    assert "missing_required_fields" in result["highlight_last_error"]


def test_invalid_ai_relevance_is_dropped_from_diagnostic_field():
    result = highlight_verdict.normalize_verdict_result(_raw(ai_relevant="unknown"))

    assert result["cluster_verdict"] == "featured"
    assert result["highlight_ai_relevant"] is None


def test_prompt_keeps_v3_7_ai_audience_scope_anchors():
    prompt = highlight_verdict.load_system_prompt()

    assert highlight_verdict.PROMPT_VERSION == "item_verdict_v3_7_2_ai_toolchain_scope_2026_06_17"
    assert "AI 投资" in prompt
    assert "AI 相关 = AI 受众相关" in prompt
    assert "GitHub/repo/开源教程" in prompt
    assert "Vercel/drop.new" in prompt
    assert "Claude Code/Codex/Cursor" in prompt
    assert "Agent 产品" in prompt
    assert "AI 工作流 CLI" in prompt
    assert "知识库/向量库" in prompt
    assert "工具名本身就是可检索线索" in prompt
    assert "原始内容是视频" in prompt
    assert "开发者/创作者相邻工具" in prompt
    assert "非直接工作流" in prompt
    assert "宽口径后的质量线" in prompt
    assert "具体对象优先" in prompt
    assert "可信来源/优质作者" in prompt
    assert "泛媒体文章" in prompt
    assert "经济学人：全球平台扩张下文化消费走向碎片化" in prompt
    assert "baoyu-design skill 新增导出可编辑 PPTX 功能" in prompt
    assert "MusicFree 开源插件化音乐播放器" in prompt
    assert "Agent 编程时代" in prompt
    assert "证券/投顾包装" in prompt
    assert "哈佛 edX 开放 CS50" in prompt
    assert "Kimi K2.7 高速版复刻墨流 Demo" in prompt
    assert "CodexGuide 最近更新了不少教程" in prompt
    assert "普通人零基础也能用 GitHub 获取资源" in prompt
    assert "飞连智能体：用 Agent 实现 Agent 办公安全" in prompt
    assert "Claude Code 桌面版汉化版发布" in prompt
    assert "刚上传一首歌变成了 Vercel 的临时网站" in prompt
    assert "Musk 预测 AI 将达到 Stockfish 级编程" in prompt
    assert "国内 AI 公司为何跑不出商业化" in prompt
    assert "飞书CLI开源库实现知识入库-装配-分发闭环" in prompt
    assert "X 帖子热传九大 AI 工具组合清单" in prompt
    assert "用 OpenCode 中转 Opus 4.6 解决 Windows 超长开机记" in prompt
    assert "Mckay Wrigley 祝贺 Cursor 三周年" in prompt
    assert "GitHub开源视频VIP解锁脚本引热议" in prompt
    assert "Samuel Hammond 发推评 AI 安全政令" in prompt
    assert "Claude 与 Codex Computer Use 能力对比" in prompt
