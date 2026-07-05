#!/usr/bin/env python3
"""
抓取 B 站"热门"+"排行榜"。

绕开 bilibili-cli 的 `bili hot` / `bili rank` — CLI 输出字段裁剪掉了 pic（封面 URL），
导致前端卡片无封面。直接调 B 站官方开放 API（不需签名、不需登录）：

- /x/web-interface/popular  → 热门（今日）
- /x/web-interface/ranking/v2?rid=0&type=all → 全站排行榜 Top 100

输出格式为裸数组 JSON（每个 item 含 bvid/title/pic/owner/stat 等），
兼容 src/ingest.py 的 _extract_bili_items + _bili_item_to_row。

见 docs/bugfix/2026-04-18.md#BF-0418-9。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("INFO2ACTION_DATA_DIR", str(BASE / "data")))
OUT_DIR = DATA_DIR / "sources" / "bilibili"


def _call_api(url: str) -> list:
    r = subprocess.run(
        ["curl", "-s", "--max-time", "15", url, "-H", "User-Agent: Mozilla/5.0"],
        capture_output=True, text=True, timeout=20,
    )
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    if payload.get("code") != 0:
        print(f"❌ API code={payload.get('code')}: {payload.get('message','')[:120]}", file=sys.stderr)
        return []
    return payload.get("data", {}).get("list", []) or []


def fetch_hot(limit: int = 30) -> int:
    items = _call_api(f"https://api.bilibili.com/x/web-interface/popular?ps={limit}&pn=1")
    path = OUT_DIR / "3-hot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    print(f"✅ 热门: {len(items)} 条 → {path.relative_to(BASE)}")
    return len(items)


def fetch_rank() -> int:
    # rid=0 全站，type=all 全部投稿；可选其他分区 rid 见 https://github.com/SocialSisterYi/bilibili-API-collect
    items = _call_api("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all")
    path = OUT_DIR / "4-rank.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    print(f"✅ 排行: {len(items)} 条 → {path.relative_to(BASE)}")
    return len(items)


def main() -> int:
    h = fetch_hot()
    r = fetch_rank()
    return 0 if (h > 0 or r > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
