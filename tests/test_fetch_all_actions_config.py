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
    assert "$SOURCE_DIR/twitter/1-following-feed.json" in text
    assert "$SOURCE_DIR/twitter/2-for-you-feed.json" in text


def test_hourly_twitter_pipeline_uses_inserted_run_scope():
    text = (ROOT / "ops" / "cron_hourly_twitter_timeline_pipeline.sh").read_text()

    assert "--run-items-scope inserted" in text
