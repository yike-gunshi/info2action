"""BF-0912-2: future Beijing publication days must not enter the timeline."""
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import remote_db
import time_utils


@pytest.mark.parametrize("now, expected", [
    ("2026-09-12T15:59:59+00:00", "2026-09-12T16:00:00+00:00"),
    ("2026-09-12T16:00:00+00:00", "2026-09-13T16:00:00+00:00"),
    ("2026-12-31T16:00:00+00:00", "2027-01-01T16:00:00+00:00"),
])
def test_publication_cutoff_uses_beijing_midnight(now, expected):
    assert time_utils.highlights_published_before(datetime.fromisoformat(now)).isoformat() == expected


class Cursor:
    def __init__(self, row=None, rows=()):
        self.row, self.rows = row, list(rows)
    def fetchone(self):
        return self.row
    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("read_model", [True, False])
def test_remote_queries_filter_before_pagination_and_count(monkeypatch, read_model):
    cutoff = datetime(2026, 9, 12, 16, tzinfo=timezone.utc)
    monkeypatch.setattr(remote_db, 'highlights_published_before', lambda: cutoff, raising=False)
    monkeypatch.setattr(remote_db, '_highlights_read_model_enabled', lambda: read_model)
    monkeypatch.setattr(remote_db, '_highlights_display_threshold', lambda: None)
    monkeypatch.setattr(remote_db, '_highlights_stale_fallback_enabled', lambda: False)
    monkeypatch.setattr(remote_db, '_fetch_event_source_metadata', lambda *a: {})
    monkeypatch.setattr(remote_db, '_read_local_read_cache', lambda *a, **kw: None)
    monkeypatch.setattr(remote_db, '_read_feed_snapshot', lambda *a, **kw: None)
    monkeypatch.setattr(remote_db, 'remote_schema', lambda: 'remote_poc')
    remote_db.clear_feed_cache_keys()
    checked = []

    class Conn:
        def execute(self, sql, params=None):
            sql = ' '.join(sql.split())
            if sql.startswith('SET LOCAL'):
                return Cursor()
            if 'highlights_read_model_state' in sql:
                return Cursor(row={'version_id': '00000000-0000-0000-0000-00000000beef',
                                   'scope_key': 'all', 'total_count': 2})
            # Both data and aggregate queries must use the SAME server cutoff.
            assert params['published_before'] == cutoff
            predicate = ('h.sort_at' if read_model else
                         'COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)')
            assert f'{predicate} < %(published_before)s' in sql
            assert sql.index(' < %(published_before)s') < min(
                [sql.rindex(x) for x in (' ORDER BY ', ' GROUP BY ', ' LIMIT ') if x in sql] or [len(sql)])
            checked.append(sql)
            if 'GROUP BY day' in sql:
                return Cursor(rows=[{'day': '2026-09-12', 'n': 1}])
            if 'count(*) AS n' in sql:
                return Cursor(row={'n': 1})
            return Cursor(rows=[])
        def commit(self):
            pass
        def rollback(self):
            pass

    @contextmanager
    def connect():
        yield Conn()
    monkeypatch.setattr(remote_db, 'connect', connect)
    result = remote_db.fetch_events(limit=10, public_only=True,
                                    since_version_snapshot=None if read_model else 0)
    assert len(checked) == (2 if read_model else 4)
    assert result['date_counts'] == {'2026-09-12': 1}
    # Materialized total includes the future article; filtered total must not.
    assert result['total_available_within_30d'] == 1
    remote_db.clear_feed_cache_keys()


def test_snapshot_keys_change_at_beijing_midnight(monkeypatch):
    cutoff = [datetime(2026, 9, 12, 16, tzinfo=timezone.utc)]
    monkeypatch.setattr(remote_db, 'highlights_published_before', lambda: cutoff[0], raising=False)
    args = dict(limit=20, public_only=True, min_github_stars=50, enabled=True, categories=[])
    first = (remote_db._events_snapshot_key(**args), remote_db._feed_events_local_cache_name(**args))
    cutoff[0] = datetime(2026, 9, 13, 16, tzinfo=timezone.utc)
    second = (remote_db._events_snapshot_key(**args), remote_db._feed_events_local_cache_name(**args))
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[0].startswith('events:v5:')
