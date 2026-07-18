from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shell_fetch_entrypoints_do_not_call_personal_x_streams():
    entrypoints = [
        "ops/fetch_all.sh",
        "ops/cron_fetch_light.sh",
        "ops/cron_fetch_twitter_timeline.sh",
        "ops/cron_hourly_twitter_timeline_pipeline.sh",
        "scripts/qa_fetch_30.sh",
        "scripts/qa_fetch_100.sh",
    ]
    forbidden = ("twitter feed", "twitter bookmarks", "twitter search")

    for relative_path in entrypoints:
        text = (ROOT / relative_path).read_text()
        assert "fetch_x_users.py" in text, relative_path
        for command in forbidden:
            assert command not in text, f"{relative_path}: {command}"


def test_python_fetch_entrypoints_do_not_call_personal_x_streams():
    routes_text = (ROOT / "src/routes/fetch.py").read_text()
    backfill_text = (ROOT / "src/backfill_since.py").read_text()
    health_text = (ROOT / "src/routes/health.py").read_text()

    assert "CLI['twitter'], 'feed'" not in routes_text
    assert "CLI['twitter'], 'search'" not in routes_text
    assert '"twitter", "feed"' not in backfill_text
    assert '"twitter", "search"' not in backfill_text
    assert '"twitter", "bookmarks"' not in backfill_text
    assert "['twitter', 'feed'" not in health_text
    assert "['twitter', '--compact', 'whoami', '--json']" in health_text
