import os
import sys
import builtins

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from routes import fetch as fetch_route


def test_derive_fetch_log_progress_uses_latest_platform_marker():
    log = """
📱 [1/3] Twitter...
  ✅ Search
🔖 [5] WayToAGI...
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'source_fetch',
        'platform': 'waytoagi',
        'percent': 34,
    }


def test_derive_fetch_log_progress_moves_to_ai_stage():
    log = """
📱 [1/3] Twitter...
================================================
  AI 统一理解...
================================================
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'ai_enrich',
        'platform': '全部平台',
        'percent': 55,
    }


def test_derive_fetch_log_progress_maps_ai_item_progress_to_global_percent():
    log = """
================================================
  AI 统一理解...
================================================
Found 800 items to enrich
  [160/800] platform=waytoagi batch size=1 ok
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'ai_enrich',
        'platform': 'waytoagi',
        'percent': 60,
    }


def test_derive_fetch_log_progress_moves_percent_after_first_ai_item():
    log = """
================================================
  AI 统一理解...
================================================
Found 800 items to enrich
  [1/800] platform=公众号 lw_69fd6d471094587ca industry/flash q=0.625
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'ai_enrich',
        'platform': '公众号',
        'percent': 56,
    }


def test_derive_fetch_log_progress_keeps_event_cluster_when_started():
    log = """
================================================
  AI 统一理解...
================================================
Found 800 items to enrich
  [400/800] platform=X batch size=1 ok
================================================
  事件聚合 (v15.0 两阶段聚类)...
================================================
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'event_cluster',
        'platform': '全部平台',
        'percent': 85,
    }


def test_derive_fetch_log_progress_does_not_treat_raw_only_footer_as_cluster():
    log = """
================================================
  抓取完成! 开始入库...
================================================

raw-only 模式：仅完成平台抓取，入库 / AI 总结 / 事件聚合交给后端 run 编排。
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'ingest',
        'platform': '全部平台',
        'percent': 38,
    }


def test_derive_fetch_log_progress_ignores_source_item_counters():
    log = """
🔖 [5] WayToAGI...
  [1/39] 为什么我们从 Claude Code 换到 Codex？丨Limitless...
  [39/39] 记忆，是 Agent 基建｜对话 Calvin@Vida...
"""

    progress = fetch_route._derive_fetch_log_progress(log)

    assert progress == {
        'stage_id': 'source_fetch',
        'platform': 'waytoagi',
        'percent': 34,
    }


def test_decorate_progress_from_log_maps_current_stage(monkeypatch, tmp_path):
    log_path = tmp_path / 'info-radar-fetch.log'
    log_path.write_text('📱 [1/3] Twitter...\n🔖 [5] WayToAGI...\n', encoding='utf-8')

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == '/tmp/info-radar-fetch.log':
            return real_open(log_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(fetch_route, 'open', fake_open, raising=False)
    progress = fetch_route._make_global_fetch_progress()
    progress['stages'][0]['status'] = 'running'

    decorated = fetch_route._decorate_progress_from_log(progress)

    assert decorated['current_stage'] == 0
    assert decorated['platform'] == 'waytoagi'
    assert decorated['percent'] == 34
    assert decorated['stages'][0]['platform'] == 'waytoagi'


def test_decorate_progress_from_log_reads_ai_enrich_log(monkeypatch, tmp_path):
    fetch_log_path = tmp_path / 'info-radar-fetch.log'
    fetch_log_path.write_text('AI 统一理解...\n', encoding='utf-8')
    ai_log_path = tmp_path / 'info-radar-ai-enrich.log'
    ai_log_path.write_text(
        'Found 800 items to enrich\n'
        '  [400/800] platform=X batch size=1 ok\n',
        encoding='utf-8',
    )

    real_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == '/tmp/info-radar-fetch.log':
            return real_open(fetch_log_path, *args, **kwargs)
        if path == '/tmp/info-radar-ai-enrich.log':
            return real_open(ai_log_path, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(fetch_route, 'open', fake_open, raising=False)
    progress = fetch_route._make_global_fetch_progress()
    progress['stages'][2]['status'] = 'running'
    progress['current_stage'] = 2

    decorated = fetch_route._decorate_progress_from_log(progress)

    assert decorated['current_stage'] == 2
    assert decorated['platform'] == 'X'
    assert decorated['percent'] == 68
    assert decorated['stages'][2]['platform'] == 'X'
