import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_fetch_all_gates_generate_actions_behind_config():
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert "auto_generate_enabled" in text
    assert "generate_actions.py" in text
    assert "自动行动点生成已关闭" in text


def test_fetch_all_gates_dedup_actions_behind_config():
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert "auto_dedup_enabled" in text
    assert "dedup_actions.py" in text
    assert "自动行动点去重已关闭" in text


def test_fetch_all_does_not_fetch_bilibili():
    """B 站已于 2026-08-06 全面下线:前端 section 早已隐藏(PR #286),抓取同步停止。"""
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert "fetch_bili_hot.py" not in text
    assert "fetch_bili_watch_later.py" not in text
    assert "$SOURCE_DIR/bilibili" not in text


def test_config_disables_bilibili_platform():
    """手动 / micro 抓取路径靠 config 开关兜底,防止绕过 fetch_all.sh 再把 B 站抓回来。"""
    import json

    cfg = json.loads((ROOT / "config" / "config.json").read_text())

    assert cfg["bilibili"]["enabled"] is False


def test_fetch_orchestrator_guards_bilibili_behind_config():
    text = (ROOT / "src" / "fetch_orchestrator.py").read_text()

    assert text.count("_is_platform_enabled('bilibili')") == 2
    assert "bilibili is disabled in config, skipping" in text


def test_fetch_all_uses_unified_enrichment():
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert "enrich_items.py" in text
    assert "--run-items-scope inserted" in text
    assert "generate_summaries.py" not in text
    assert "score_items.py" not in text


def test_fetch_all_respects_per_run_data_dir():
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert 'DATA_DIR="${INFO2ACTION_DATA_DIR:-$BASE/data}"' in text
    assert 'SOURCE_DIR="${INFO2ACTION_SOURCE_DIR:-$DATA_DIR/sources}"' in text
    assert "fetch_x_users.py" in text


def test_fetch_all_runs_x_user_channel():
    text = (ROOT / "ops" / "fetch_all.sh").read_text()

    assert "fetch_x_users.py" in text


def test_hourly_twitter_pipeline_uses_inserted_run_scope():
    text = (ROOT / "ops" / "cron_hourly_twitter_timeline_pipeline.sh").read_text()

    assert "--run-items-scope inserted" in text
