import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.mark.parametrize(("query", "expected"), [
    ("我想学习seedance 2.5", '("seedance2.5" OR "seedance 2.5" OR "seedance-2.5")'),
    ("GPT-6 Astra 最新动态", '("GPT6" OR "GPT 6" OR "GPT-6") "Astra" "最新动态"'),
    ("豆包工作怎么用", '"豆包工作怎么用"'),
    ("我想搜索最新", '"我想搜索最新"'),
    ("", '""'),
    ("  \t ", '""'),
    ("　ＧＰＴ－６　教程　", '("GPT6" OR "GPT 6" OR "GPT-6")'),
    ("gpt 6", '("gpt6" OR "gpt 6" OR "gpt-6")'),
    ("seedance2.5", '("seedance2.5" OR "seedance 2.5" OR "seedance-2.5")'),
    ("搜索 机器学习 和 深度学习 资源 教程", '"机器学习" "深度学习" "资源"'),
    ("搜索词内搜索器", '"词内搜索器"'),
    ("模型 和谐 与 工具", '"模型" "和谐" "工具"'),
    ('a"b \\path OR', '"a\\"b" "\\\\path" "OR"'),
])
def test_lexical_query(query, expected):
    from remote_db.ai_search import lexical_query

    assert lexical_query(query) == expected
