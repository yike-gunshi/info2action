"""Tests for ranking category weights and legacy alias handling."""
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import ranking  # noqa: E402


@pytest.mark.skip(
    reason="v3.1 alias 行为已被 v4.0 废弃。canonicalize_category 现在把 ai_tools→efficiency_tools "
           "(L1 改名),tools→efficiency_tools。ranking.py 的 _ROLE_CATEGORY_WEIGHTS 还在 TODO "
           "(CLF-V4-RANK)等 PM 给新 L1 权重。本套件需在 ranking 重构后重写。"
)
def test_profile_to_weights_uses_new_taxonomy_keys():
    weights = ranking.profile_to_weights({
        'role': 'developer',
        'interests': [],
        'tools': [],
    })['category_weights']

    assert 'ai_tools' in weights
    assert 'tech' in weights
    assert 'tools' not in weights
    assert 'insights' not in weights


@pytest.mark.skip(
    reason="v3.1 alias 行为已被 v4.0 废弃。tools/insights 现在被 canonicalize 成 efficiency_tools/tech "
           "(不是 ai_tools/tech)。本测试需在 v4 ranking 重构后重写。"
)
def test_compute_match_score_accepts_legacy_aliases():
    user_weights = {
        'category_weights': {'ai_tools': 2.5, 'tech': 1.8, 'other': 1.0},
        'keyword_boosts': [],
    }

    assert ranking.compute_match_score({'ai_category': 'tools'}, user_weights) == 2.5
    assert ranking.compute_match_score({'ai_category': 'insights'}, user_weights) == 1.8
