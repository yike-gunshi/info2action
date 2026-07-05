import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from time_utils import to_utc_iso, sort_key  # noqa: E402


def test_normalizes_rfc2822_to_utc_iso():
    assert to_utc_iso('Wed, 22 Apr 2026 16:11:23 +0000') == '2026-04-22T16:11:23Z'


def test_normalizes_timezone_aware_iso_to_utc_iso():
    assert to_utc_iso('2026-04-26T01:52:57+00:00') == '2026-04-26T01:52:57Z'


def test_interprets_naive_project_timestamps_as_beijing_time():
    assert to_utc_iso('2026-04-26 09:35') == '2026-04-26T01:35:00Z'
    assert to_utc_iso('2026-04-26T10:06:46.428874') == '2026-04-26T02:06:46Z'


def test_sort_key_orders_mixed_formats_by_real_time():
    assert sort_key('2026-04-26 09:35') > sort_key('Wed, 22 Apr 2026 16:11:23 +0000')
