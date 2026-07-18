"""Keep the configured X source registry mirrored into configured X Lists."""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import shutil
import subprocess

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    with open(os.path.join(BASE, "config", "config.json"), encoding="utf-8") as handle:
        CONFIG = json.load(handle)
except (OSError, ValueError):
    CONFIG = {}


def _twitter_config():
    value = CONFIG.get("twitter", {})
    return value if isinstance(value, dict) else {}


def list_id():
    definitions = list_definitions()
    return definitions[0]["list_id"] if len(definitions) == 1 else None


def list_definitions():
    raw = os.environ.get("INFO2ACTION_X_LISTS_JSON")
    if raw:
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = []
    else:
        values = _twitter_config().get("x_lists")

    definitions = []
    seen_keys = set()
    seen_ids = set()
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        configured_id = str(item.get("list_id") or "").strip()
        if not key or not configured_id.isdigit() or key in seen_keys or configured_id in seen_ids:
            continue
        seen_keys.add(key)
        seen_ids.add(configured_id)
        definitions.append({
            "key": key,
            "name": str(item.get("name") or key).strip() or key,
            "list_id": configured_id,
        })
    if definitions:
        return definitions

    value = os.environ.get("INFO2ACTION_X_LIST_ID") or _twitter_config().get("x_list_id")
    configured_id = str(value or "").strip()
    if configured_id.isdigit():
        return [{"key": "default", "name": "X List", "list_id": configured_id}]
    return []


def list_fetch_count():
    value = os.environ.get("INFO2ACTION_X_LIST_FETCH_COUNT") or _twitter_config().get(
        "list_fetch_count", 500
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 500
    return max(1, min(500, parsed))


def _sync_interval_seconds():
    value = _twitter_config().get("list_sync_interval_seconds", 2)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 2
    return max(0, min(30, parsed))


def _sync_retry_cooldown_seconds():
    value = _twitter_config().get("list_sync_retry_cooldown_seconds", 21600)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 21600
    return max(0, min(86400, parsed))


def _fetch_sync_per_list():
    value = _twitter_config().get("list_fetch_sync_per_list", 1)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(0, min(5, parsed))


def _state_path():
    return os.path.join(BASE, "data", "x_list_registry.json")


@contextmanager
def _exclusive_registry_lock():
    path = f"{_state_path()}.lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state():
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(state):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _registry_handles(sources):
    seen = set()
    handles = []
    for source in sources:
        handle = str(source["source_key"] or "").strip().lstrip("@")
        key = handle.casefold()
        if not handle or key in seen:
            continue
        seen.add(key)
        handles.append(handle)
    return handles


def _source_value(source, key):
    try:
        return source[key]
    except (KeyError, TypeError):
        return None


def _source_list_key(source):
    value = _source_value(source, "config_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = None
    if not isinstance(value, dict):
        return None
    key = str(value.get("x_list_key") or "").strip()
    return key or None


def _handles_by_list(sources, definitions):
    if len(definitions) == 1:
        return {definitions[0]["key"]: _registry_handles(sources)}, []

    definition_keys = {item["key"] for item in definitions}
    grouped = {item["key"]: [] for item in definitions}
    unassigned = []
    seen = set()
    for source in sources:
        handle = str(_source_value(source, "source_key") or "").strip().lstrip("@")
        handle_key = handle.casefold()
        if not handle or handle_key in seen:
            continue
        seen.add(handle_key)
        group_key = _source_list_key(source)
        if group_key in definition_keys:
            grouped[group_key].append(handle)
        else:
            unassigned.append(handle)
    return grouped, unassigned


def _definition_state(state, definition, *, single):
    lists = state.get("lists") if isinstance(state.get("lists"), dict) else {}
    item = lists.get(definition["key"])
    if isinstance(item, dict) and str(item.get("list_id") or "") == definition["list_id"]:
        return item
    if single and str(state.get("list_id") or "") == definition["list_id"]:
        return state
    return {}


def status_for_sources(sources):
    definitions = list_definitions()
    handles = _registry_handles(sources)
    grouped, unassigned = _handles_by_list(sources, definitions)
    state = _read_state()
    single = len(definitions) == 1
    list_statuses = []
    all_synced_keys = set()
    for definition in definitions:
        item_state = _definition_state(state, definition, single=single)
        synced_keys = {
            str(handle).strip().lstrip("@").casefold()
            for handle in item_state.get("synced_handles") or []
            if str(handle).strip()
        }
        group_handles = grouped.get(definition["key"], [])
        synced = [handle for handle in group_handles if handle.casefold() in synced_keys]
        pending = [handle for handle in group_handles if handle.casefold() not in synced_keys]
        all_synced_keys.update(handle.casefold() for handle in synced)
        list_statuses.append({
            "key": definition["key"],
            "name": definition["name"],
            "list_id": definition["list_id"],
            "list_url": f"https://x.com/i/lists/{definition['list_id']}",
            "registry_count": len(group_handles),
            "synced_count": len(synced),
            "pending_count": len(pending),
            "synced_handles": synced,
            "pending_handles": pending,
            "last_synced_at": item_state.get("last_synced_at"),
            "last_error": item_state.get("last_error"),
        })

    configured_id = definitions[0]["list_id"] if single else None
    synced = [handle for handle in handles if handle.casefold() in all_synced_keys]
    pending = [handle for handle in handles if handle.casefold() not in all_synced_keys]
    latest_syncs = [item["last_synced_at"] for item in list_statuses if item["last_synced_at"]]
    errors = [item["last_error"] for item in list_statuses if item["last_error"]]
    result = {
        "configured": bool(definitions),
        "mode": "list",
        "list_id": configured_id,
        "list_url": f"https://x.com/i/lists/{configured_id}" if configured_id else None,
        "registry_count": len(handles),
        "synced_count": len(synced),
        "pending_count": len(pending),
        "synced_handles": synced,
        "pending_handles": pending,
        "last_synced_at": max(latest_syncs) if latest_syncs else None,
        "last_error": "; ".join(errors) if errors else None,
    }
    if not single:
        result["lists"] = list_statuses
        result["unassigned_handles"] = unassigned
    return result


def _twitter_tool_python():
    binary = shutil.which("twitter")
    if not binary:
        raise RuntimeError("twitter-cli is not installed")
    try:
        with open(os.path.realpath(binary), encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect twitter-cli launcher: {exc}") from exc
    if not first_line.startswith("#!"):
        raise RuntimeError("twitter-cli launcher has no Python shebang")
    python = first_line[2:].strip()
    if not python or not os.path.exists(python):
        raise RuntimeError("twitter-cli Python runtime is unavailable")
    return python


def _run_bridge(handles, *, configured_id, interval):
    command = [
        _twitter_tool_python(),
        os.path.join(BASE, "src", "x_list_bridge.py"),
        "--list-id",
        configured_id,
        "--interval",
        str(interval),
    ]
    for handle in handles:
        command.extend(["--handle", handle])

    env = os.environ.copy()
    if not env.get("TWITTER_PROXY"):
        proxy = (
            env.get("https_proxy")
            or env.get("HTTPS_PROXY")
            or env.get("http_proxy")
            or env.get("HTTP_PROXY")
        )
        if proxy:
            env["TWITTER_PROXY"] = proxy
    timeout = max(120, len(handles) * (interval + 40))
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError as exc:
        message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise RuntimeError(f"X List bridge returned invalid JSON: {message[:500]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("X List bridge returned an invalid payload")
    if result.returncode != 0 and not isinstance(payload.get("results"), list):
        message = payload.get("error") or result.stderr or f"exit {result.returncode}"
        raise RuntimeError(str(message)[:500])
    return payload


def _sync_registry_members_locked(sources, *, full=False, max_targets_per_list=None):
    definitions = list_definitions()
    if not definitions:
        raise RuntimeError("twitter.x_lists is not configured")

    grouped, _unassigned = _handles_by_list(sources, definitions)
    state = _read_state()
    single = len(definitions) == 1
    failed = []
    next_lists = {}
    now = _utc_now()
    for definition in definitions:
        handles = grouped.get(definition["key"], [])
        item_state = _definition_state(state, definition, single=single)
        existing = item_state.get("synced_handles") or []
        synced_by_key = {
            str(handle).strip().lstrip("@").casefold(): str(handle).strip().lstrip("@")
            for handle in existing
            if str(handle).strip()
        }
        targets = handles if full else [
            handle for handle in handles if handle.casefold() not in synced_by_key
        ]
        if max_targets_per_list is not None:
            targets = targets[:max_targets_per_list]
        list_failed = []
        if targets:
            payload = _run_bridge(
                targets,
                configured_id=definition["list_id"],
                interval=_sync_interval_seconds(),
            )
            results = payload.get("results") if isinstance(payload.get("results"), list) else []
            by_key = {
                str(item.get("handle") or "").casefold(): item
                for item in results
                if isinstance(item, dict) and item.get("handle")
            }
            for handle in targets:
                result = by_key.get(handle.casefold())
                if result and result.get("ok") is True:
                    synced_by_key[handle.casefold()] = handle
                else:
                    failure = {
                        "handle": handle,
                        "error": str((result or {}).get("error") or "member sync returned no result")[:500],
                    }
                    if not single:
                        failure["list_key"] = definition["key"]
                    list_failed.append(failure)
        failed.extend(list_failed)
        ordered_synced = [handle for handle in handles if handle.casefold() in synced_by_key]
        next_lists[definition["key"]] = {
            "list_id": definition["list_id"],
            "synced_handles": ordered_synced,
            "last_synced_at": now,
            "last_error": f"{len(list_failed)} member sync failed" if list_failed else None,
        }

    next_state = {"schema_version": 2, "lists": next_lists}
    if single:
        only = definitions[0]
        only_state = next_lists[only["key"]]
        next_state.update({
            "list_id": only["list_id"],
            "synced_handles": only_state["synced_handles"],
            "last_synced_at": only_state["last_synced_at"],
            "last_error": only_state["last_error"],
        })
    _write_state(next_state)
    status = status_for_sources(sources)
    status["failed"] = failed
    return status


def sync_registry_members(sources, *, full=False):
    with _exclusive_registry_lock():
        return _sync_registry_members_locked(sources, full=full)


def sync_registry_members_for_fetch(sources):
    with _exclusive_registry_lock():
        status = status_for_sources(sources)
        if status["pending_count"] and status.get("last_error"):
            try:
                last_synced = datetime.fromisoformat(
                    str(status.get("last_synced_at") or "").replace("Z", "+00:00")
                )
            except ValueError:
                last_synced = None
            if last_synced is not None:
                if last_synced.tzinfo is None:
                    last_synced = last_synced.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_synced).total_seconds()
                if age < _sync_retry_cooldown_seconds():
                    status["failed"] = []
                    status["sync_skipped_reason"] = "cooldown"
                    return status
        return _sync_registry_members_locked(
            sources,
            full=False,
            max_targets_per_list=_fetch_sync_per_list(),
        )
