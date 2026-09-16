import base64
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import fetch_lingowhale as lw  # noqa: E402
from utils import email  # noqa: E402


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_token_store_stays_persistent_while_output_follows_run_data_dir(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.join(BASE, "src")
    env["INFO2ACTION_DATA_DIR"] = str(tmp_path / "run")
    env.pop("INFO2ACTION_LINGOWHALE_TOKEN_STORE", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, fetch_lingowhale as lw; "
                "print(json.dumps({'token': lw._token_store_path(), 'out': lw.OUT_DIR}))"
            ),
        ],
        cwd=BASE,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = json.loads(result.stdout.strip().splitlines()[-1])

    assert paths["token"] == os.path.join(BASE, "data", "lingowhale_tokens.json")
    assert paths["out"] == os.path.join(str(tmp_path / "run"), "lingowhale")


def test_main_proactively_refreshes_when_auth_token_expires_within_three_days(monkeypatch):
    now = time.time()
    refreshes = []
    alerts = []
    monkeypatch.setattr(
        lw,
        "_current_token_fields",
        lambda: {
            "access_token": _jwt(now + 7 * 86400),
            "auth_token": _jwt(now + 2 * 86400),
        },
    )
    monkeypatch.setattr(lw, "refresh_lingowhale_tokens", lambda timeout=30: refreshes.append(timeout) or True)
    monkeypatch.setattr(lw, "fetch_groups", lambda: ({}, []))
    monkeypatch.setattr(lw, "fetch_subscription_feed", lambda *args, **kwargs: [])
    monkeypatch.setattr(lw, "_alert_lingowhale", lambda *args, **kwargs: alerts.append(args) or True)

    lw.main()

    assert refreshes == [30]
    assert alerts == []


def test_main_alerts_when_expiring_auth_token_refresh_fails(monkeypatch):
    now = time.time()
    alerts = []
    monkeypatch.setattr(
        lw,
        "_current_token_fields",
        lambda: {
            "access_token": _jwt(now + 7 * 86400),
            "auth_token": _jwt(now + 2 * 86400),
        },
    )
    monkeypatch.setattr(lw, "refresh_lingowhale_tokens", lambda timeout=30: False)
    monkeypatch.setattr(lw, "_alert_lingowhale", lambda *args: alerts.append(args) or True)
    monkeypatch.setattr(lw, "fetch_groups", lambda: ({}, []))
    monkeypatch.setattr(lw, "fetch_subscription_feed", lambda *args, **kwargs: [])

    lw.main()

    assert len(alerts) == 1
    assert alerts[0][0] == "auth_expiring"
    assert "人工重新登录语鲸" in alerts[0][2]


def test_failed_refresh_is_cached_until_cooldown_expires(monkeypatch):
    now = [1_000_000.0]
    requests = []
    monkeypatch.setattr(lw.time, "time", lambda: now[0])
    monkeypatch.setattr(lw, "_LAST_REFRESH_OK", False)
    monkeypatch.setattr(lw, "_LAST_REFRESH_FAILED_AT", None, raising=False)
    monkeypatch.setattr(lw, "_current_token_fields", lambda: {})
    monkeypatch.setattr(
        lw,
        "_raw_post_json",
        lambda *args, **kwargs: requests.append(args[0]) or {"code": 995, "msg": "expired"},
    )

    assert lw.refresh_lingowhale_tokens() is False
    assert len(requests) == 1

    now[0] += 299
    assert lw.refresh_lingowhale_tokens() is False
    assert len(requests) == 1

    now[0] += 2
    assert lw.refresh_lingowhale_tokens() is False
    assert len(requests) == 2


def test_main_does_not_proactively_refresh_when_tokens_have_enough_time(monkeypatch):
    now = time.time()
    refreshes = []
    monkeypatch.setattr(
        lw,
        "_current_token_fields",
        lambda: {
            "access_token": _jwt(now + 2 * 86400),
            "auth_token": _jwt(now + 4 * 86400),
        },
    )
    monkeypatch.setattr(lw, "refresh_lingowhale_tokens", lambda timeout=30: refreshes.append(timeout) or True)
    monkeypatch.setattr(lw, "fetch_groups", lambda: ({}, []))
    monkeypatch.setattr(lw, "fetch_subscription_feed", lambda *args, **kwargs: [])

    lw.main()

    assert refreshes == []


def test_main_proactively_refreshes_when_access_token_expires_within_24_hours(monkeypatch):
    now = time.time()
    refreshes = []
    monkeypatch.setattr(
        lw,
        "_current_token_fields",
        lambda: {
            "access_token": _jwt(now + 23 * 3600),
            "auth_token": _jwt(now + 7 * 86400),
        },
    )
    monkeypatch.setattr(lw, "refresh_lingowhale_tokens", lambda timeout=30: refreshes.append(timeout) or True)
    monkeypatch.setattr(lw, "fetch_groups", lambda: ({}, []))
    monkeypatch.setattr(lw, "fetch_subscription_feed", lambda *args, **kwargs: [])

    lw.main()

    assert refreshes == [30]


def test_token_error_and_failed_refresh_sends_credential_alert(monkeypatch):
    alerts = []
    responses = [{"code": 10010, "msg": "token expired"}]
    monkeypatch.setattr(lw, "_raw_post_json", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(lw, "refresh_lingowhale_tokens", lambda timeout=30: False)
    monkeypatch.setattr(lw, "_alert_lingowhale", lambda *args: alerts.append(args) or True)

    result = lw._post_json("/api/lingowhale/v1/feed/subscription", {})

    assert result["code"] == 10010
    assert alerts[0][0] == "credential_refresh_failed"


def test_feed_code_10010_records_all_active_sources_failed(monkeypatch):
    records = []
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"channel-a": 11, "channel-b": 12})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(lw, "_post_json", lambda *args, **kwargs: {"code": 10010, "msg": "token expired"})
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, str(error))),
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    assert lw.fetch_subscription_feed() == []

    assert {source_id for source_id, ok, _ in records if not ok} == {11, 12}
    assert not [record for record in records if record[1]]


def test_all_channels_successful_with_no_fresh_entries_records_no_failures(monkeypatch):
    records = []
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"quiet-a": 31, "quiet-b": 32})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(
        lw,
        "_fetch_subscription_feed_from_endpoint",
        lambda *args, **kwargs: ([], 1, "no fresh entries"),
    )
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, error)),
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    assert lw.fetch_subscription_feed(since_ts=1_000) == []

    assert records == [(31, True, None), (32, True, None)]


def test_all_channels_page_one_request_errors_record_all_active_sources_failed(monkeypatch):
    records = []
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"failed-a": 41, "failed-b": 42})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(
        lw,
        "_fetch_feed_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("request failed")),
    )
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, str(error))),
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    assert lw.fetch_subscription_feed() == []

    assert [(source_id, ok) for source_id, ok, _ in records] == [(41, False), (42, False)]
    assert all("全部频道请求失败" in error for _, _, error in records)


def test_partial_channel_failure_records_each_result_without_global_failure(monkeypatch):
    records = []
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"healthy": 51, "failed": 52})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        if channel_ids == ["failed"]:
            raise RuntimeError("one channel failed")
        return ([], 1, "no fresh entries")

    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, str(error) if error else None)),
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    assert lw.fetch_subscription_feed() == []

    assert records == [(51, True, None), (52, False, "one channel failed")]


def test_one_channel_with_no_entries_is_not_failed_when_feed_has_entries(monkeypatch):
    records = []
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"quiet": 21, "updated": 22})
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: [])
    monkeypatch.setattr(
        lw,
        "_fetch_subscription_feed_from_endpoint",
        lambda endpoint, channel_ids, label, timeout=30, since_ts=None: (
            ([{"entry_id": "new", "pub_time": 1}] if channel_ids == ["updated"] else []),
            1,
            "done",
        ),
    )
    monkeypatch.setattr(
        lw,
        "_record_lingowhale_result",
        lambda source_id, *, ok, error=None: records.append((source_id, ok, error)),
    )
    monkeypatch.setattr(lw.time, "sleep", lambda seconds: None)

    lw.fetch_subscription_feed()

    assert (21, True, None) in records
    assert not [record for record in records if record[0] == 21 and not record[1]]


def test_lingowhale_alert_is_deduplicated_for_24_hours(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(email, "RESEND_API_KEY", "test-key")
    monkeypatch.setattr(email.resend.Emails, "send", lambda payload: sent.append(payload))
    state_path = tmp_path / "alerts.json"

    assert email.send_lingowhale_alert(
        "auth_expiring",
        "语鲸 auth_token 即将过期",
        "剩余不足 3 天",
        state_path=str(state_path),
        now=1_000_000,
    ) is True
    assert email.send_lingowhale_alert(
        "auth_expiring",
        "语鲸 auth_token 即将过期",
        "剩余不足 3 天",
        state_path=str(state_path),
        now=1_000_000 + 86399,
    ) is False

    assert len(sent) == 1
