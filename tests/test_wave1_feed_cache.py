import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import remote_db
from routes import fetch as fetch_route


def test_post_fetch_read_model_prewarm_does_not_clear_feed_cache(monkeypatch):
    prewarm_calls = []
    clear_calls = []
    threads = []
    original_thread = fetch_route.threading.Thread

    class CapturingThread(original_thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            threads.append(self)

    monkeypatch.setenv('INFO2ACTION_CACHE_PREWARM', '1')
    monkeypatch.setenv('INFO2ACTION_PREWARM_PLATFORMS', '1')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL', '1')
    monkeypatch.setenv('INFO2ACTION_INFO_READ_MODEL_REFRESH', '1')
    monkeypatch.setenv('INFO2ACTION_HIGHLIGHTS_READ_MODEL', '0')
    monkeypatch.setattr(fetch_route, '_remote_db_pressure_skip_reason', lambda: None)
    monkeypatch.setattr(fetch_route.threading, 'Thread', CapturingThread)
    monkeypatch.setattr(
        fetch_route.remote_db,
        'prewarm_platforms',
        lambda **kwargs: prewarm_calls.append(kwargs) or {'ok': True},
    )
    monkeypatch.setattr(
        fetch_route,
        '_clear_feed_caches_safely',
        lambda: clear_calls.append(True),
    )

    fetch_route._schedule_post_fetch_read_model_refresh(42)

    assert len(threads) == 1
    threads[0].join(timeout=1)
    assert not threads[0].is_alive()
    assert prewarm_calls == [
        {
            'refresh_read_model': True,
            'refresh_read_model_min_interval_sec': 600,
            'refresh_highlights_read_model': False,
            'refresh_highlights_read_model_min_interval_sec': 600,
        }
    ]
    assert clear_calls == []


def test_feed_result_cache_uses_independent_default_ttl(monkeypatch):
    monkeypatch.delenv('INFO2ACTION_FEED_RESULT_CACHE_TTL_SEC', raising=False)

    assert remote_db._feed_result_cache_ttl_sec() == 900
    assert remote_db._feed_result_cache_ttl_sec() >= 600
    assert remote_db._feed_result_cache_ttl({'read_model': 'info_platforms_v1'}) == 900
    assert remote_db._feed_result_cache_ttl({'degraded': True}) == 0
