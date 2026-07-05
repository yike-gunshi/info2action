#!/usr/bin/env python3
"""
抓取 B 站"稍后再看"列表。

绕开 bilibili-cli v0.6.2 的 `bili watch-later` bug（返回 count=N 但 items=[]），
直接调 B 站官方 API `/x/v2/history/toview`，只需 SESSDATA cookie。

Credential 源：~/.bilibili-cli/credential.json（bili CLI 登录后生成）。
输出：data/sources/bilibili/watch-later.json（裸数组格式，兼容 ingest _extract_bili_items）。

见 docs/bugfix/2026-04-18.md#BF-0418-9。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CRED_FILE = Path.home() / ".bilibili-cli" / "credential.json"
DATA_DIR = Path(os.environ.get("INFO2ACTION_DATA_DIR", str(BASE / "data")))
OUT_FILE = DATA_DIR / "sources" / "bilibili" / "watch-later.json"


def main() -> int:
    if not CRED_FILE.exists():
        print(f"❌ 找不到 bili CLI credential: {CRED_FILE}", file=sys.stderr)
        print("   请先运行 `bili login` 扫码登录", file=sys.stderr)
        return 1

    cred = json.loads(CRED_FILE.read_text())
    sessdata = cred.get("sessdata", "")
    if not sessdata:
        print("❌ credential.json 里没有 sessdata", file=sys.stderr)
        return 1

    r = subprocess.run(
        ["curl", "-s", "--max-time", "15",
         "https://api.bilibili.com/x/v2/history/toview",
         "-H", f"Cookie: SESSDATA={sessdata}",
         "-H", "User-Agent: Mozilla/5.0",
         "-H", "Referer: https://www.bilibili.com"],
        capture_output=True, text=True, timeout=20,
    )

    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"❌ 响应解析失败: {r.stdout[:300]}", file=sys.stderr)
        return 1

    code = payload.get("code")
    if code != 0:
        print(f"❌ B 站 API 返回 code={code}: {payload.get('message','')}", file=sys.stderr)
        if code == -101:
            print("   SESSDATA 失效，请重新 `bili login`", file=sys.stderr)
        return 1

    items = payload.get("data", {}).get("list", []) or []

    # 合成字段：ingest 用 _add_at_iso 作为 fetched_at，前端按"加入稍后再看时间"倒序展示
    from datetime import datetime, timezone
    for item in items:
        add_at = item.get("add_at")
        if isinstance(add_at, (int, float)) and add_at > 0:
            item["_add_at_iso"] = datetime.fromtimestamp(add_at, tz=timezone.utc).isoformat()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    print(f"✅ 稍后再看: 抓到 {len(items)} 条 → {OUT_FILE.relative_to(BASE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
