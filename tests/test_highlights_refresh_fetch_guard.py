from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import remote_db  # noqa: E402


def _reset_throttle():
    remote_db._HIGHLIGHTS_READ_MODEL_REFRESH_LAST_ATTEMPT_AT = None


def test_skips_refresh_when_fetch_running():
    _reset_throttle()
    with mock.patch.object(remote_db, "_highlights_read_model_enabled", return_value=True), \
         mock.patch.object(remote_db, "_highlights_refresh_skip_during_fetch_enabled", return_value=True), \
         mock.patch.object(remote_db, "has_recent_running_fetch_remote", return_value=True), \
         mock.patch.object(remote_db, "refresh_highlights_read_model_delta_in_place") as delta, \
         mock.patch.object(remote_db, "refresh_highlights_read_model") as full:
        res = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)
    assert res.get("skipped") == "fetch_running"
    delta.assert_not_called()
    full.assert_not_called()


def test_proceeds_when_no_fetch_running():
    _reset_throttle()
    with mock.patch.object(remote_db, "_highlights_read_model_enabled", return_value=True), \
         mock.patch.object(remote_db, "_highlights_refresh_skip_during_fetch_enabled", return_value=True), \
         mock.patch.object(remote_db, "has_recent_running_fetch_remote", return_value=False), \
         mock.patch.object(remote_db, "_highlights_read_model_incremental_enabled", return_value=True), \
         mock.patch.object(remote_db, "refresh_highlights_read_model_delta_in_place",
                           return_value={"ok": True, "mode": "delta"}) as delta:
        res = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)
    assert res.get("mode") == "delta"
    delta.assert_called_once()


def test_guard_disabled_proceeds_and_does_not_probe():
    _reset_throttle()
    with mock.patch.object(remote_db, "_highlights_read_model_enabled", return_value=True), \
         mock.patch.object(remote_db, "_highlights_refresh_skip_during_fetch_enabled", return_value=False), \
         mock.patch.object(remote_db, "has_recent_running_fetch_remote", return_value=True) as guard, \
         mock.patch.object(remote_db, "_highlights_read_model_incremental_enabled", return_value=True), \
         mock.patch.object(remote_db, "refresh_highlights_read_model_delta_in_place",
                           return_value={"ok": True, "mode": "delta"}) as delta:
        res = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)
    assert res.get("mode") == "delta"
    guard.assert_not_called()  # flag off -> never probe running-run
    delta.assert_called_once()


def test_fail_open_when_running_probe_errors():
    _reset_throttle()
    with mock.patch.object(remote_db, "_highlights_read_model_enabled", return_value=True), \
         mock.patch.object(remote_db, "_highlights_refresh_skip_during_fetch_enabled", return_value=True), \
         mock.patch.object(remote_db, "has_recent_running_fetch_remote",
                           side_effect=RuntimeError("db down")), \
         mock.patch.object(remote_db, "_highlights_read_model_incremental_enabled", return_value=True), \
         mock.patch.object(remote_db, "refresh_highlights_read_model_delta_in_place",
                           return_value={"ok": True, "mode": "delta"}) as delta:
        res = remote_db.refresh_highlights_read_model_if_stale(min_interval_sec=0)
    assert res.get("mode") == "delta"  # fail open -> proceeded despite probe error
    delta.assert_called_once()
