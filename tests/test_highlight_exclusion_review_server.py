from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_server_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "scripts" / "highlight_exclusion_review_server.py"
    spec = importlib.util.spec_from_file_location("highlight_exclusion_review_server", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_page_exposes_completion_feedback_and_shortcuts():
    server = _load_server_module()
    html = server.HTML

    assert 'data-verdict="should_feature"' in html
    assert 'data-verdict="confirmed_drop"' in html
    assert 'data-verdict="unsure"' in html
    assert "已保存：" in html
    assert "nextUnreviewed" in html
    assert "document.addEventListener('keydown'" in html
    assert "saveReview('should_feature')" in html
    assert "saveReview('confirmed_drop')" in html
    assert "saveReview('unsure')" in html
    assert "F</kbd>" in html
    assert "D</kbd>" in html
    assert "U</kbd>" in html
    assert 'id="clusterVerdict"' in html
    assert 'value="drop"' in html
    assert 'id="recentDays"' in html
    assert 'value="3"' in html
    assert 'id="rowLimit"' in html
    assert 'value="200"' in html
    assert 'value="500"' in html
    assert "cluster_verdict=" in html
    assert "recent_days=" in html
    assert "safeUrl(d.url || '')" in html
    assert 'rel="noopener noreferrer"' in html
