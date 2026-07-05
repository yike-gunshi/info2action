from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ingest  # noqa: E402


def test_twitter_timeline_fetch_script_excludes_search_and_other_sources():
    text = (ROOT / "ops" / "cron_fetch_twitter_timeline.sh").read_text()

    assert "twitter feed -t following" in text
    assert "twitter feed -t for-you" in text
    assert "twitter search" not in text
    assert "xhs " not in text
    assert "fetch_bili" not in text
    assert "fetch_feeds.py" not in text
    assert "fetch_lingowhale.py" not in text
    assert "fetch_waytoagi.py" not in text


def test_hourly_twitter_timeline_pipeline_sets_pythonpath_for_inline_db_imports():
    text = (ROOT / "ops" / "cron_hourly_twitter_timeline_pipeline.sh").read_text()

    assert 'export PYTHONPATH="$BASE/src:${PYTHONPATH:-}"' in text
    assert "scripts/run_ready_backfill_windows.py" in text


def test_ingest_twitter_timeline_only_skips_search_files(tmp_path, monkeypatch):
    twitter_dir = tmp_path / "data" / "sources" / "twitter"
    twitter_dir.mkdir(parents=True)
    (twitter_dir / "search-AI.json").write_text("[]")

    seen = []

    def fake_safe_load_json(path):
        seen.append(Path(path).name)
        return []

    monkeypatch.setattr(ingest, "BASE", str(tmp_path))
    monkeypatch.setattr(ingest, "safe_load_json", fake_safe_load_json)
    monkeypatch.setattr(ingest, "_extract_twitter_posters_inline", lambda tasks: None)
    monkeypatch.setattr(ingest, "_run_asr_for_twitter_videos_inline", lambda conn, tweet_ids: None)

    ingest.ingest_twitter(conn=object(), timeline_only=True)

    assert seen == ["1-following-feed.json", "2-for-you-feed.json"]


def test_ingest_twitter_full_mode_skips_retired_search_files(tmp_path, monkeypatch):
    twitter_dir = tmp_path / "data" / "sources" / "twitter"
    twitter_dir.mkdir(parents=True)
    (twitter_dir / "3-ai-search.json").write_text("[]")
    (twitter_dir / "search-AI.json").write_text("[]")

    seen = []

    def fake_safe_load_json(path):
        seen.append(Path(path).name)
        return []

    monkeypatch.setattr(ingest, "BASE", str(tmp_path))
    monkeypatch.setattr(ingest, "safe_load_json", fake_safe_load_json)
    monkeypatch.setattr(ingest, "_extract_twitter_posters_inline", lambda tasks: None)
    monkeypatch.setattr(ingest, "_run_asr_for_twitter_videos_inline", lambda conn, tweet_ids: None)

    ingest.ingest_twitter(conn=object(), timeline_only=False)

    assert seen == [
        "1-following-feed.json",
        "2-for-you-feed.json",
        "4-bookmarks.json",
    ]
