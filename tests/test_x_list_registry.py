from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


def _sources(*handles):
    return [
        {"id": index + 1, "source_key": handle}
        for index, handle in enumerate(handles)
    ]


def _grouped_sources(*pairs):
    return [
        {
            "id": index + 1,
            "source_key": handle,
            "config_json": json.dumps({"x_list_key": list_key}),
        }
        for index, (handle, list_key) in enumerate(pairs)
    ]


def test_status_for_sources_reports_each_configured_list_and_unassigned_sources(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {
            "x_lists": [
                {"key": "official", "name": "i2a · AI Official", "list_id": "111"},
                {"key": "people", "name": "i2a · AI People", "list_id": "222"},
            ],
        },
    })
    (tmp_path / "x_list_registry.json").write_text(json.dumps({
        "schema_version": 2,
        "lists": {
            "official": {
                "list_id": "111",
                "synced_handles": ["OpenAI"],
                "last_synced_at": "2026-07-11T00:00:00+00:00",
                "last_error": None,
            },
            "people": {
                "list_id": "222",
                "synced_handles": [],
                "last_synced_at": None,
                "last_error": None,
            },
        },
    }))

    status = x_list_registry.status_for_sources(_grouped_sources(
        ("openai", "official"),
        ("karpathy", "people"),
        ("unassigned", "missing"),
    ))

    assert status["configured"] is True
    assert status["registry_count"] == 3
    assert status["synced_handles"] == ["openai"]
    assert status["pending_handles"] == ["karpathy", "unassigned"]
    assert status["unassigned_handles"] == ["unassigned"]
    assert [(item["key"], item["registry_count"], item["synced_count"]) for item in status["lists"]] == [
        ("official", 1, 1),
        ("people", 1, 0),
    ]


def test_sync_registry_members_routes_each_source_to_its_configured_list(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {
            "x_lists": [
                {"key": "official", "name": "Official", "list_id": "111"},
                {"key": "people", "name": "People", "list_id": "222"},
            ],
            "list_sync_interval_seconds": 0,
        },
    })
    calls = []

    def fake_bridge(handles, *, configured_id, interval):
        calls.append((configured_id, list(handles), interval))
        return {
            "ok": True,
            "results": [{"handle": handle, "ok": True} for handle in handles],
        }

    monkeypatch.setattr(x_list_registry, "_run_bridge", fake_bridge)

    result = x_list_registry.sync_registry_members(_grouped_sources(
        ("openai", "official"),
        ("karpathy", "people"),
        ("unassigned", "missing"),
    ))

    assert calls == [("111", ["openai"], 0), ("222", ["karpathy"], 0)]
    assert result["synced_handles"] == ["openai", "karpathy"]
    assert result["pending_handles"] == ["unassigned"]
    state = json.loads((tmp_path / "x_list_registry.json").read_text())
    assert state["schema_version"] == 2
    assert state["lists"]["official"]["synced_handles"] == ["openai"]
    assert state["lists"]["people"]["synced_handles"] == ["karpathy"]


def test_registry_state_path_stays_persistent_when_run_output_dir_changes(
    tmp_path, monkeypatch
):
    import x_list_registry

    project_dir = tmp_path / "project"
    run_dir = tmp_path / "run_sources" / "3889"
    monkeypatch.setattr(x_list_registry, "BASE", str(project_dir))
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(run_dir))

    assert x_list_registry._state_path() == str(project_dir / "data" / "x_list_registry.json")


def test_incremental_sync_reuses_persistent_state_from_inside_run_directory(
    tmp_path, monkeypatch
):
    import x_list_registry

    project_dir = tmp_path / "project"
    state_dir = project_dir / "data"
    state_dir.mkdir(parents=True)
    (state_dir / "x_list_registry.json").write_text(json.dumps({
        "schema_version": 1,
        "list_id": "123456",
        "synced_handles": ["alpha"],
        "last_synced_at": "2026-07-11T00:00:00+00:00",
        "last_error": None,
    }))
    monkeypatch.setattr(x_list_registry, "BASE", str(project_dir))
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path / "run_sources" / "3889"))
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {"x_list_id": "123456", "list_sync_interval_seconds": 0},
    })
    invocations = []

    def fake_bridge(handles, **_kwargs):
        invocations.append(list(handles))
        return {
            "ok": True,
            "results": [{"handle": handle, "ok": True} for handle in handles],
        }

    monkeypatch.setattr(x_list_registry, "_run_bridge", fake_bridge)

    result = x_list_registry.sync_registry_members(_sources("alpha", "beta"))

    assert invocations == [["beta"]]
    assert result["synced_handles"] == ["alpha", "beta"]


def test_status_for_sources_reports_registry_membership_case_insensitively(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {"x_list_id": "123456", "list_fetch_count": 500},
    })
    (tmp_path / "x_list_registry.json").write_text(json.dumps({
        "schema_version": 1,
        "list_id": "123456",
        "last_synced_at": "2026-07-11T00:00:00+00:00",
        "synced_handles": ["Alpha", "beta"],
        "last_error": None,
    }))

    status = x_list_registry.status_for_sources(_sources("alpha", "BETA", "quiet"))

    assert status == {
        "configured": True,
        "mode": "list",
        "list_id": "123456",
        "list_url": "https://x.com/i/lists/123456",
        "registry_count": 3,
        "synced_count": 2,
        "pending_count": 1,
        "synced_handles": ["alpha", "BETA"],
        "pending_handles": ["quiet"],
        "last_synced_at": "2026-07-11T00:00:00+00:00",
        "last_error": None,
    }


def test_sync_registry_members_persists_success_and_exposes_partial_failure(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {"x_list_id": "123456", "list_sync_interval_seconds": 0},
    })
    calls = []

    def fake_run(args, capture_output, text, timeout, env):
        calls.append((args, timeout, env.get("TWITTER_PROXY")))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "ok": False,
                "list_id": "123456",
                "results": [
                    {"handle": "alpha", "ok": True, "member_count": 1},
                    {"handle": "beta", "ok": False, "error": "HTTP 429"},
                ],
            }),
            stderr="",
        )

    monkeypatch.setattr(x_list_registry, "_twitter_tool_python", lambda: "/tool/python")
    monkeypatch.setattr(x_list_registry.subprocess, "run", fake_run)

    result = x_list_registry.sync_registry_members(
        _sources("alpha", "beta"),
        full=False,
    )

    assert result["synced_handles"] == ["alpha"]
    assert result["pending_handles"] == ["beta"]
    assert result["synced_count"] == 1
    assert result["pending_count"] == 1
    assert result["failed"] == [{"handle": "beta", "error": "HTTP 429"}]
    assert result["last_error"] == "1 member sync failed"
    assert calls[0][0][:2] == ["/tool/python", os.path.join(BASE, "src", "x_list_bridge.py")]
    assert "alpha" in calls[0][0]
    assert "beta" in calls[0][0]

    state = json.loads((tmp_path / "x_list_registry.json").read_text())
    assert state["list_id"] == "123456"
    assert state["synced_handles"] == ["alpha"]


def test_sync_registry_members_is_incremental_unless_full_requested(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {"x_list_id": "123456", "list_sync_interval_seconds": 0},
    })
    (tmp_path / "x_list_registry.json").write_text(json.dumps({
        "schema_version": 1,
        "list_id": "123456",
        "synced_handles": ["alpha"],
        "last_synced_at": "2026-07-11T00:00:00+00:00",
        "last_error": None,
    }))
    invocations = []

    def fake_bridge(handles, **_kwargs):
        invocations.append(handles)
        return {
            "ok": True,
            "results": [{"handle": handle, "ok": True} for handle in handles],
        }

    monkeypatch.setattr(x_list_registry, "_run_bridge", fake_bridge)

    incremental = x_list_registry.sync_registry_members(_sources("alpha", "beta"))
    full = x_list_registry.sync_registry_members(_sources("alpha", "beta"), full=True)

    assert invocations == [["beta"], ["alpha", "beta"]]
    assert incremental["synced_count"] == 2
    assert full["synced_count"] == 2


def test_sync_registry_members_serializes_concurrent_incremental_sync(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {"x_list_id": "123456", "list_sync_interval_seconds": 0},
    })
    invocations = []

    def fake_bridge(handles, **_kwargs):
        invocations.append(list(handles))
        time.sleep(0.05)
        return {
            "ok": True,
            "results": [{"handle": handle, "ok": True} for handle in handles],
        }

    monkeypatch.setattr(x_list_registry, "_run_bridge", fake_bridge)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: x_list_registry.sync_registry_members(_sources("alpha", "beta")),
            range(2),
        ))

    assert invocations == [["alpha", "beta"]]
    assert [result["synced_count"] for result in results] == [2, 2]


def test_fetch_sync_respects_cooldown_after_member_write_limit(tmp_path, monkeypatch):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {
            "x_list_id": "123456",
            "list_sync_retry_cooldown_seconds": 21600,
        },
    })
    (tmp_path / "x_list_registry.json").write_text(json.dumps({
        "schema_version": 1,
        "list_id": "123456",
        "synced_handles": ["alpha"],
        "last_synced_at": x_list_registry._utc_now(),
        "last_error": "1 member sync failed",
    }))
    monkeypatch.setattr(
        x_list_registry,
        "_run_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("bridge called")),
    )

    result = x_list_registry.sync_registry_members_for_fetch(_sources("alpha", "beta"))

    assert result["synced_handles"] == ["alpha"]
    assert result["pending_handles"] == ["beta"]
    assert result["sync_skipped_reason"] == "cooldown"


def test_fetch_sync_only_probes_one_pending_member_per_configured_list(
    tmp_path, monkeypatch
):
    import x_list_registry

    monkeypatch.setattr(
        x_list_registry, "_state_path", lambda: str(tmp_path / "x_list_registry.json")
    )
    monkeypatch.setattr(x_list_registry, "CONFIG", {
        "twitter": {
            "x_lists": [
                {"key": "official", "name": "Official", "list_id": "111"},
                {"key": "people", "name": "People", "list_id": "222"},
            ],
            "list_fetch_sync_per_list": 1,
            "list_sync_interval_seconds": 0,
        },
    })
    calls = []

    def fake_bridge(handles, *, configured_id, interval):
        calls.append((configured_id, list(handles)))
        return {
            "ok": False,
            "results": [
                {"handle": handle, "ok": False, "error": "member write limited"}
                for handle in handles
            ],
        }

    monkeypatch.setattr(x_list_registry, "_run_bridge", fake_bridge)

    result = x_list_registry.sync_registry_members_for_fetch(_grouped_sources(
        ("openai", "official"),
        ("anthropic", "official"),
        ("karpathy", "people"),
        ("ylecun", "people"),
    ))

    assert calls == [("111", ["openai"]), ("222", ["karpathy"])]
    assert result["pending_count"] == 4
