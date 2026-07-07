"""BF-0706-5: parse_actions_response 对截断/畸形 JSON 的抢救。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import generate_actions as ga


def test_parses_clean_array():
    text = '[{"title": "做A", "steps": ["s1"]}]'
    res = ga.parse_actions_response(text)
    assert len(res) == 1 and res[0]["title"] == "做A"


def test_salvages_truncated_array():
    # 第一个 action 完整,第二个在 reason 里被截断(M3 思考+输出超 max_tokens 高发)
    text = (
        '[\n  {"title": "撰写科普文", "action_type": "content", '
        '"steps": ["读原文", "查资料", "写初稿"], "prompt": "完整可执行指令"},\n'
        '  {"title": "第二个行动", "reason": [{"label": "价值", "text": "这里被截断'
    )
    res = ga.parse_actions_response(text)
    assert len(res) == 1, res
    assert res[0]["title"] == "撰写科普文"
    assert len(res[0]["steps"]) == 3


def test_salvages_trailing_garbage_after_array():
    text = '[{"title": "只要这个"}]  然后模型多说了一段废话 {不是合法json'
    res = ga.parse_actions_response(text)
    assert len(res) == 1 and res[0]["title"] == "只要这个"


def test_empty_and_none():
    assert ga.parse_actions_response("") == []
    assert ga.parse_actions_response("[]") == []
