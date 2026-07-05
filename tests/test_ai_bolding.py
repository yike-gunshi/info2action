import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_count_bold_spans_counts_direct_markdown_output():
    from ai_bolding import count_bold_spans

    assert count_bold_spans("**OpenAI** 发布 **GPT-5**。") == 2
    assert count_bold_spans("OpenAI 发布 GPT-5。") == 0


def test_item_bolding_stats_reads_llm_markdown_without_candidates():
    from ai_bolding import summarize_item_bolding

    stats = summarize_item_bolding(
        "用户吐槽 **AI 味** 前端。",
        [{"title": "问题", "points": ["典型特征是 **Inter 字体** 和紫蓝渐变"]}],
    )

    assert stats["summary_bold_spans"] == 1
    assert stats["body_bold_spans"] == 1
    assert stats["has_non_heading_bold"] is True


def test_bolding_stats_distinguishes_heading_only_from_body_bold():
    from ai_bolding import summarize_cluster_bolding

    heading_only = (
        "【精华速览】\n软银股价上涨，孙正义财富增加。\n\n"
        "【全文拆解】\n**软银与孙正义**\n- 软银股价单日大涨。\n"
    )
    with_body = (
        "【精华速览】\n**软银**股价上涨，孙正义财富增加。\n\n"
        "【全文拆解】\n**软银与孙正义**\n- **OpenAI** 投资收益推高账面资产。\n"
    )

    assert summarize_cluster_bolding(heading_only)["only_heading_bold"] is True
    stats = summarize_cluster_bolding(with_body)
    assert stats["only_heading_bold"] is False
    assert stats["summary_bold_spans"] == 1
    assert stats["body_bold_spans"] == 1
