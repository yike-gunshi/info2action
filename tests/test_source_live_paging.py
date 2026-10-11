import pytest
import remote_db
import json
from types import SimpleNamespace
from tests.test_source_live_feed import Conn, Result, card, install, clean


def test_page_probe_and_scoped_cursor_do_not_recount(monkeypatch):
    conn = Conn()
    install(monkeypatch, conn)
    first = remote_db.query_feed_by_platform(platform='twitter', limit=2, public_only=True)
    assert first['total'] == 90
    assert first['has_more'] is True
    assert len(first['items']) == 2
    cursor = first['next_cursor']
    assert cursor['after']['id'] == 'b'
    conn.calls.clear()
    remote_db.query_feed_by_platform(platform='twitter', limit=2, public_only=True, cursor=cursor)
    sql, params = next((s,p) for s,p in conn.calls if 'items i' in s)
    assert 'OFFSET' not in sql
    assert 'cursor_id' in params
    with pytest.raises(ValueError, match='scope'):
        remote_db.query_feed_by_platform(platform='github', public_only=True, cursor=cursor)
    with pytest.raises(ValueError, match='scope'):
        remote_db.query_feed_by_platform(platform='twitter', user_id='other', cursor=cursor)


def test_unknown_total_has_more_uses_probe_not_cached_count(monkeypatch):
    conn = Conn(counts=False)
    install(monkeypatch, conn)
    page = remote_db.query_feed_by_platform(platform='twitter', limit=2, public_only=True)
    assert page['total'] is None and page['counts_generated_at'] is None
    assert page['has_more'] is True and len(page['items']) == 2
    conn.cards = [card('last')]
    last = remote_db.query_feed_by_platform(platform='twitter', limit=2, public_only=True)
    assert last['has_more'] is False and last['next_cursor'] is None


def test_new_cursor_overrides_offset_but_legacy_offset_keeps_its_position(monkeypatch):
    conn = Conn()
    install(monkeypatch, conn)
    page = remote_db.query_feed_by_platform(platform='twitter', offset=80, limit=2, public_only=True)
    sql, params = next((s,p) for s,p in conn.calls if 'items i' in s)
    assert 'OFFSET %(offset)s' in sql and params['offset'] == 80
    assert page['next_offset'] == 82
    conn.calls.clear()
    remote_db.query_feed_by_platform(platform='twitter', offset=82, cursor=page['next_cursor'], limit=2, public_only=True)
    sql, params = next((s,p) for s,p in conn.calls if 'items i' in s)
    assert 'OFFSET' not in sql and 'cursor_id' in params


@pytest.mark.parametrize('cursor', [
    '{bad', {'kind': 'live', 'v': 2}, {'kind': 'live', 'v': 1, 'scope_key': 'wrong', 'after': {}},
])
def test_route_rejects_invalid_source_cursor_with_400(monkeypatch, cursor):
    import routes.feed as route
    monkeypatch.setattr(route.remote_db, 'feed_read_from_remote', lambda: True)
    monkeypatch.setattr(route, '_github_display_min_stars', lambda: 50)
    request = SimpleNamespace(method='GET', headers={}, state=SimpleNamespace(user=None, legacy_authenticated=False))
    response = route.get_feed_platforms_more(request, platform='twitter', offset=0, limit=50,
        source=None, group=None, category=None, search=None, exclude_ids=None,
        cursor=cursor if isinstance(cursor, str) else json.dumps(cursor))
    assert response.status_code == 400


def test_live_cursor_parser_does_not_change_type_read_model_parser():
    import routes.feed as route
    old = {'version_id': 'v', 'scope_key': 'legacy', 'rank_after': 51}
    assert route._optional_source_cursor(json.dumps(old)) == route._optional_read_model_cursor(json.dumps(old)) == old


def test_legacy_cursor_fallback_preserves_rank_when_source_read_model_missing(monkeypatch):
    conn = Conn()
    install(monkeypatch, conn)
    monkeypatch.setattr(remote_db, '_query_feed_by_platform_read_model', lambda **k: None)
    page = remote_db.query_feed_by_platform(platform='twitter', limit=2, public_only=True,
        cursor={'version_id': 'old-version', 'scope_key': 'platform=twitter|dimension=all|value=', 'rank_after': 80})
    assert page['offset'] == 80
    assert page['next_cursor']['kind'] == 'live'
