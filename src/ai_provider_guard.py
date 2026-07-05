"""Shared AI provider cooldown guard.

The guard keeps provider-level rate-limit state outside any single script so a
429 in one high-volume job can stop the rest of the jobs from hammering MiniMax.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


BASE_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = str(BASE_DIR / "data" / "ai_provider_state.json")
LOCK_PATH = str(BASE_DIR / "data" / "ai_provider_state.lock")
MINIMAX_CHAT_PROVIDER = "minimax-chat"
MINIMAX_EMBEDDING_PROVIDER = "minimax-embedding"
DEFAULT_PROVIDER = MINIMAX_CHAT_PROVIDER
DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("INFO2ACTION_AI_COOLDOWN_SECONDS", "1800"))
LOCAL_TZ = timezone(timedelta(hours=8))


class ProviderCooldown(RuntimeError):
    def __init__(self, provider: str, cooldown_until: str | None, message: str):
        super().__init__(message)
        self.provider = provider
        self.cooldown_until = cooldown_until


class ProviderActionRequired(RuntimeError):
    def __init__(self, provider: str, action: str | None, message: str):
        super().__init__(message)
        self.provider = provider
        self.action = action


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_provider(provider: str | None = None) -> str:
    value = (provider or DEFAULT_PROVIDER).strip()
    if value == "minimax":
        return MINIMAX_CHAT_PROVIDER
    return value or DEFAULT_PROVIDER


def _blank_state(provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    return {
        "provider": provider,
        "status": "ok",
        "consecutive_429": 0,
        "updated_at": _iso(_now()),
    }


def _ensure_parent(path: str) -> None:
    parent = Path(path).parent
    if parent:
        parent.mkdir(parents=True, exist_ok=True)


def _read_all_state_unlocked() -> dict[str, Any]:
    path = Path(STATE_PATH)
    if not path.exists():
        return {"providers": {}}

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"providers": {}}

    if isinstance(raw.get("providers"), dict):
        providers: dict[str, Any] = {}
        for key, value in raw["providers"].items():
            if not isinstance(value, dict):
                continue
            provider = _normalize_provider(value.get("provider") or key)
            state = dict(value)
            state["provider"] = provider
            state.setdefault("status", "ok")
            state.setdefault("consecutive_429", 0)
            providers[provider] = state
        return {"providers": providers}

    # Legacy file shape before provider scopes were split:
    # {"provider":"minimax","status":"cooldown",...}
    if isinstance(raw, dict) and "status" in raw:
        provider = _normalize_provider(raw.get("provider"))
        state = dict(raw)
        state["provider"] = provider
        state.setdefault("consecutive_429", 0)
        return {"providers": {provider: state}}

    return {"providers": {}}


def _read_state_unlocked(provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    state = _read_all_state_unlocked().get("providers", {}).get(provider)
    if not state:
        return _blank_state(provider)
    state = dict(state)
    state.setdefault("provider", provider)
    state.setdefault("status", "ok")
    state.setdefault("consecutive_429", 0)
    return state


def _write_state_unlocked(state: dict[str, Any]) -> dict[str, Any]:
    provider = _normalize_provider(state.get("provider"))
    state = dict(state)
    state["provider"] = provider
    all_state = _read_all_state_unlocked()
    providers = dict(all_state.get("providers") or {})
    providers[provider] = state
    payload = {"providers": providers}

    _ensure_parent(STATE_PATH)
    directory = Path(STATE_PATH).parent
    fd, tmp_path = tempfile.mkstemp(prefix=".ai_provider_state.", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, STATE_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return state


@contextmanager
def _state_lock():
    _ensure_parent(LOCK_PATH)
    with open(LOCK_PATH, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _with_state_lock(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    with _state_lock():
        return fn()


def load_state(provider: str = DEFAULT_PROVIDER) -> dict[str, Any]:
    return _with_state_lock(lambda: _read_state_unlocked(provider))


def is_cooldown_active(provider: str = DEFAULT_PROVIDER, now: datetime | None = None) -> bool:
    state = load_state(provider)
    if state.get("status") != "cooldown":
        return False

    cooldown_until = _parse_iso(state.get("cooldown_until"))
    if cooldown_until is None:
        return True
    return (now or _now()) < cooldown_until


def is_action_required(provider: str = DEFAULT_PROVIDER) -> bool:
    return load_state(provider).get("status") == "action_required"


def _local_text(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M (UTC+8)")


def _default_cooldown_message(provider: str, cooldown_until: datetime | None, source: str) -> str:
    if provider == MINIMAX_CHAT_PROVIDER and cooldown_until:
        return f"MiniMax chat Token Plan 本窗口已用尽，等待到 {_local_text(cooldown_until)} 后继续"
    if provider == MINIMAX_EMBEDDING_PROVIDER and cooldown_until:
        return "MiniMax embedding 已禁用；生产 embedding 请使用 OpenRouter + OPENROUTER_API_KEY"
    cooldown = _iso(cooldown_until) if cooldown_until else "unknown"
    return f"{provider} provider is cooling down until {cooldown} after rate limit in {source}"


def _default_action_message(provider: str, action: str | None) -> str:
    if provider == MINIMAX_EMBEDDING_PROVIDER and action == "recharge_embedding":
        return "MiniMax embedding 已禁用；生产 embedding 请使用 OpenRouter + OPENROUTER_API_KEY"
    return f"{provider} provider action required: {action or 'unknown'}"


def provider_message(provider: str = DEFAULT_PROVIDER) -> str:
    state = load_state(provider)
    if state.get("user_message"):
        return state["user_message"]
    source = state.get("last_source") or "unknown"
    provider = _normalize_provider(state.get("provider"))
    if state.get("status") == "action_required":
        return _default_action_message(provider, state.get("action"))
    cooldown_until = _parse_iso(state.get("cooldown_until"))
    return _default_cooldown_message(provider, cooldown_until, source)


def cooldown_message(provider: str = DEFAULT_PROVIDER) -> str:
    return provider_message(provider)


def ensure_provider_available(provider: str = DEFAULT_PROVIDER, source: str | None = None) -> None:
    provider = _normalize_provider(provider)
    state = load_state(provider)
    if state.get("status") == "action_required":
        message = provider_message(provider)
        if source:
            message = f"{message}; blocked source={source}"
        raise ProviderActionRequired(provider, state.get("action"), message)
    if not is_cooldown_active(provider):
        return
    message = provider_message(provider)
    if source:
        message = f"{message}; blocked source={source}"
    raise ProviderCooldown(provider, state.get("cooldown_until"), message)


def record_rate_limit(
    provider: str = DEFAULT_PROVIDER,
    *,
    source: str = "unknown",
    cooldown_seconds: int | None = None,
    error: str | None = None,
    action: str | None = None,
    user_message: str | None = None,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)
    seconds = cooldown_seconds if cooldown_seconds is not None else DEFAULT_COOLDOWN_SECONDS

    def update() -> dict[str, Any]:
        previous = _read_state_unlocked(provider)
        now = _now()
        cooldown_until = now + timedelta(seconds=seconds)
        state = {
            "provider": provider,
            "status": "cooldown",
            "action": action or "rate_limit",
            "consecutive_429": int(previous.get("consecutive_429", 0)) + 1,
            "last_error": error or "HTTP 429 rate limit",
            "last_error_at": _iso(now),
            "last_source": source,
            "cooldown_seconds": seconds,
            "cooldown_until": _iso(cooldown_until),
            "updated_at": _iso(now),
        }
        if user_message:
            state["user_message"] = user_message
        elif action == "wait_until_reset":
            state["user_message"] = _default_cooldown_message(provider, cooldown_until, source)
        return _write_state_unlocked(state)

    return _with_state_lock(update)


def record_action_required(
    provider: str = DEFAULT_PROVIDER,
    *,
    action: str,
    source: str = "unknown",
    error: str | None = None,
    user_message: str | None = None,
) -> dict[str, Any]:
    provider = _normalize_provider(provider)

    def update() -> dict[str, Any]:
        now = _now()
        message = user_message or _default_action_message(provider, action)
        state = {
            "provider": provider,
            "status": "action_required",
            "action": action,
            "last_error": error or action,
            "last_error_at": _iso(now),
            "last_source": source,
            "user_message": message,
            "updated_at": _iso(now),
            "consecutive_429": 0,
        }
        return _write_state_unlocked(state)

    return _with_state_lock(update)


def record_success(provider: str = DEFAULT_PROVIDER, *, source: str = "unknown") -> dict[str, Any]:
    provider = _normalize_provider(provider)

    def update() -> dict[str, Any]:
        now = _now()
        state = {
            "provider": provider,
            "status": "ok",
            "consecutive_429": 0,
            "last_success_at": _iso(now),
            "last_source": source,
            "updated_at": _iso(now),
        }
        return _write_state_unlocked(state)

    return _with_state_lock(update)


def _parse_retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
    value = None
    if headers:
        try:
            value = headers.get("Retry-After")
        except AttributeError:
            value = None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, AttributeError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, retry_at.timestamp() - _now().timestamp())


def read_http_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except Exception:
        return ""


def parse_minimax_reset_seconds(body: str) -> float | None:
    if not body:
        return None
    seconds_match = re.search(r"resets at [^()]+\((\d+)\)", body)
    if seconds_match:
        return max(0.0, float(seconds_match.group(1)))
    iso_match = re.search(r"resets at ([0-9T:+\\-]+)", body)
    if not iso_match:
        return None
    try:
        reset_at = datetime.fromisoformat(iso_match.group(1))
    except ValueError:
        return None
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return max(0.0, reset_at.timestamp() - _now().timestamp())


def classify_minimax_chat_http_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    body = read_http_error_body(exc)
    retry_after = _parse_retry_after_seconds(exc)
    reset_after = parse_minimax_reset_seconds(body)
    wait_seconds = reset_after if reset_after is not None else retry_after
    body_lower = body.lower()
    is_token_plan = (
        exc.code == 429
        and (
            "usage limit exceeded" in body_lower
            or "resets at" in body_lower
            or "token plan" in body_lower
        )
    )
    action = "wait_until_reset" if is_token_plan else "rate_limit"
    return {
        "provider": MINIMAX_CHAT_PROVIDER,
        "action": action,
        "body": body,
        "retry_after_seconds": wait_seconds,
        "error": f"HTTP {exc.code}: {exc.reason}",
    }


def is_minimax_embedding_recharge_error(error: str | None) -> bool:
    text = (error or "").lower()
    return "1008" in text or "insufficient balance" in text or "余额不足" in text


def is_minimax_embedding_rate_limit_error(error: str | None) -> bool:
    text = (error or "").lower()
    return "1002" in text or "rate limit" in text or "too many requests" in text


def guarded_urlopen(
    request: Any,
    *,
    provider: str = DEFAULT_PROVIDER,
    source: str = "unknown",
    timeout: float | None = None,
    allow_probe: bool = False,
    record_429: bool = True,
    **kwargs: Any,
) -> Any:
    provider = _normalize_provider(provider)
    if not allow_probe:
        ensure_provider_available(provider, source=source)

    try:
        if timeout is None:
            response = urllib.request.urlopen(request, **kwargs)
        else:
            response = urllib.request.urlopen(request, timeout=timeout, **kwargs)
    except urllib.error.HTTPError as exc:
        if exc.code == 429 and record_429:
            record_rate_limit(provider, source=source, error=f"HTTP {exc.code}: {exc.reason}")
        raise

    if allow_probe or not is_cooldown_active(provider):
        record_success(provider, source=source)
    return response
