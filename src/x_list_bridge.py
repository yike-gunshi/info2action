#!/usr/bin/env python3
"""Run X List member writes inside twitter-cli's isolated uv environment."""
from __future__ import annotations

import argparse
import json
import time


def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list-id", required=True)
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--handle", action="append", default=[])
    return parser.parse_args()


def main():
    args = _args()
    from twitter_cli.cli import _get_client
    from twitter_cli.client import _get_cffi_session, _url_fetch
    from twitter_cli.config import load_config
    from twitter_cli.graphql import _resolve_query_id

    client = _get_client(load_config(), quiet=True)
    query_id = _resolve_query_id(
        "ListAddMember",
        prefer_fallback=False,
        url_fetch_fn=_url_fetch,
    )
    session = _get_cffi_session()
    results = []

    for index, handle in enumerate(args.handle):
        try:
            profile = client.fetch_user(handle)
            url = f"https://x.com/i/api/graphql/{query_id}/ListAddMember"
            response = session.post(
                url,
                headers=client._build_headers(url=url, method="POST"),
                json={
                    "variables": {"listId": str(args.list_id), "userId": str(profile.id)},
                    "queryId": query_id,
                },
                timeout=30,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            list_data = ((payload or {}).get("data") or {}).get("list")
            if response.ok and isinstance(list_data, dict):
                results.append({
                    "handle": handle,
                    "ok": True,
                    "user_id": str(profile.id),
                    "member_count": list_data.get("member_count"),
                })
            else:
                errors = (payload or {}).get("errors") or []
                message = "; ".join(
                    str(item.get("message") or item)
                    for item in errors
                    if isinstance(item, dict)
                ) or response.text[:300] or f"HTTP {response.status_code}"
                results.append({"handle": handle, "ok": False, "error": message})
        except Exception as exc:  # one handle must not abort the remaining registry
            results.append({"handle": handle, "ok": False, "error": str(exc)[:500]})
        if index < len(args.handle) - 1 and args.interval > 0:
            time.sleep(args.interval)

    payload = {
        "ok": all(item.get("ok") is True for item in results),
        "list_id": str(args.list_id),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
