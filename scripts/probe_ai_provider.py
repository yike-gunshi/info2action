#!/usr/bin/env python3
"""Probe AI provider recovery during cooldown windows."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
sys.path.insert(0, SRC_DIR)

import ai_provider_guard
from env_utils import load_project_env


CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _apply_project_env() -> None:
    for key, value in load_project_env(BASE_DIR).items():
        os.environ.setdefault(key, value)


def should_probe(only_if_cooldown: bool, provider: str = ai_provider_guard.MINIMAX_CHAT_PROVIDER) -> bool:
    if not only_if_cooldown:
        return True
    return (
        ai_provider_guard.is_cooldown_active(provider)
        or ai_provider_guard.is_action_required(provider)
    )


def probe_minimax(ai_config: dict) -> bool:
    project_env = load_project_env(BASE_DIR)
    api_key = (
        os.environ.get("MINIMAX_API_KEY")
        or project_env.get("MINIMAX_API_KEY")
        or ai_config.get("api_key", "")
    ).strip()
    api_base = (
        os.environ.get("MINIMAX_API_BASE")
        or project_env.get("MINIMAX_API_BASE")
        or ai_config.get("api_base", "https://api.minimaxi.com/anthropic/v1")
    ).strip().rstrip("/")
    model = (
        os.environ.get("MINIMAX_MODEL")
        or project_env.get("MINIMAX_MODEL")
        or ai_config.get("model", "MiniMax-M2.7")
    ).strip()
    if not api_key:
        raise ValueError("missing ai_summary.api_key")

    url = f"{api_base}/messages"
    payload = json.dumps({
        "model": model,
        "system": "Return exactly: OK",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    })

    ctx = ssl.create_default_context()
    with ai_provider_guard.guarded_urlopen(
        req,
        provider=ai_provider_guard.MINIMAX_CHAT_PROVIDER,
        source="ai_provider_probe",
        timeout=30,
        context=ctx,
        allow_probe=True,
    ) as resp:
        resp.read()
    return True


def probe_minimax_embedding(config: dict) -> bool:
    print("MiniMax embedding probe disabled: production embedding uses OpenRouter")
    ai_provider_guard.record_success(
        ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER,
        source="embedding_provider_probe_disabled",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe AI provider cooldown recovery")
    parser.add_argument("--only-if-cooldown", action="store_true")
    parser.add_argument(
        "--provider",
        choices=("chat", "embedding", "all"),
        default="all",
        help="which MiniMax quota scope to probe",
    )
    args = parser.parse_args()

    _apply_project_env()

    config = load_config()
    exit_code = 0

    if args.provider in ("chat", "all"):
        if not should_probe(args.only_if_cooldown, ai_provider_guard.MINIMAX_CHAT_PROVIDER):
            print("MiniMax chat probe skipped: provider is not cooling down")
        else:
            ai_config = config.get("ai_summary", {})
            provider = ai_config.get("provider", "minimax")
            if provider != "minimax":
                print(f"AI provider is {provider}; MiniMax chat probe skipped")
            else:
                try:
                    probe_minimax(ai_config)
                    print("MiniMax chat probe: recovered")
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        classified = ai_provider_guard.classify_minimax_chat_http_error(e)
                        retry_after = classified.get("retry_after_seconds")
                        ai_provider_guard.record_rate_limit(
                            ai_provider_guard.MINIMAX_CHAT_PROVIDER,
                            source="ai_provider_probe",
                            cooldown_seconds=int(retry_after or ai_provider_guard.DEFAULT_COOLDOWN_SECONDS),
                            error=classified.get("error") or f"HTTP {e.code}: {e.reason}",
                            action=classified.get("action") or "rate_limit",
                        )
                        print(ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_CHAT_PROVIDER))
                        exit_code = max(exit_code, 2)
                    else:
                        print(f"MiniMax chat probe HTTP error: {e.code}")
                        exit_code = max(exit_code, 1)
                except Exception as e:
                    print(f"MiniMax chat probe failed: {str(e)[:120]}")
                    exit_code = max(exit_code, 1)

    if args.provider in ("embedding", "all"):
        if not should_probe(args.only_if_cooldown, ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER):
            print("MiniMax embedding probe skipped: provider is not blocked")
        else:
            try:
                probe_minimax_embedding(config)
                print("MiniMax embedding probe: disabled")
            except ai_provider_guard.ProviderActionRequired:
                print(ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER))
                exit_code = max(exit_code, 3)
            except ai_provider_guard.ProviderCooldown:
                print(ai_provider_guard.provider_message(ai_provider_guard.MINIMAX_EMBEDDING_PROVIDER))
                exit_code = max(exit_code, 2)
            except Exception as e:
                print(f"MiniMax embedding probe failed: {str(e)[:120]}")
                exit_code = max(exit_code, 1)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
