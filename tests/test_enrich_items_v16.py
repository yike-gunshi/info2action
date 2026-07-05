"""W2.T5 (v16.0 频道精简): enrich prompt 拼接 GitHub README 行为。

覆盖场景:
- GitHub item + 非空 detail_json.readme → user prompt 拼接 README 段落
- 非 GitHub item (twitter/rss) → user prompt 不拼 README
- 超长 README (>80k) → 截到 GITHUB_README_ENRICH_MAX_CHARS,从尾部保留
- detail_json.readme 为空字符串 / 字段缺失 / 非法 JSON → 不报错且不拼空段
"""

from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_github_item(readme: str | None, *, item_id: str = "gh-1") -> dict:
    detail = {
        "stars": 1234,
        "language": "Python",
    }
    if readme is not None:
        detail["readme"] = readme
    return {
        "id": item_id,
        "platform": "github",
        "source": "trending_zh",
        "author_name": "owner",
        "title": "owner/repo - awesome thing",
        "content": "Stars: 1234, Lang: Python",
        "url": "https://github.com/owner/repo",
        "metrics_json": json.dumps({"stars": 1234}),
        "detail_json": json.dumps(detail, ensure_ascii=False),
        "asr_text": "",
    }


def test_enrich_prompt_includes_readme_for_github():
    import enrich_items

    readme = "# Awesome Repo\n\n这是一个开源教程,讲如何用 Python 抓取数据。" * 20
    item = _make_github_item(readme)

    out = enrich_items.build_item_content(item)

    assert "【完整 README】" in out, "GitHub item must inline README marker"
    # README content (or its tail) must appear in the prompt.
    assert "开源教程" in out
    # Title metadata still present.
    assert "owner/repo" in out


def test_enrich_prompt_omits_readme_for_non_github():
    import enrich_items

    for platform in ("twitter", "rss", "reddit", "hackernews"):
        item = {
            "id": f"{platform}-1",
            "platform": platform,
            "source": "unit",
            "author_name": "alice",
            "title": "标题",
            "content": "正文内容",
            "url": "https://example.com/x",
            "metrics_json": None,
            # Even if a non-github platform somehow carries detail_json.readme,
            # we must not splice it (avoid leaking unrelated content).
            "detail_json": json.dumps({"readme": "ghost README leakage"}),
            "asr_text": "",
        }
        out = enrich_items.build_item_content(item)
        assert "【完整 README】" not in out, f"{platform} must not inline README"
        assert "ghost README leakage" not in out, f"{platform} must not leak README payload"


def test_enrich_prompt_truncates_long_readme():
    import enrich_items

    cap = enrich_items.GITHUB_README_ENRICH_MAX_CHARS  # 80_000
    # 90k chars README → must be tail-truncated to <= cap.
    head_marker = "HEAD_SENTINEL_should_be_dropped"
    tail_marker = "TAIL_SENTINEL_must_survive"
    middle = "x" * (cap + 10_000)  # ensures total > cap by ~10k
    long_readme = head_marker + middle + tail_marker

    item = _make_github_item(long_readme, item_id="gh-long")
    out = enrich_items.build_item_content(item)

    assert "【完整 README】" in out
    # Tail-truncation: head dropped, tail kept.
    assert head_marker not in out, "head must be dropped on tail-truncation"
    assert tail_marker in out, "tail must survive truncation"
    # And the README slice in the prompt must not exceed the cap.
    readme_segment = out.split("【完整 README】\n", 1)[1]
    assert len(readme_segment) <= cap, (
        f"README segment {len(readme_segment)} chars exceeds cap {cap}"
    )


def test_enrich_prompt_handles_empty_readme():
    import enrich_items

    # Case A: detail_json.readme is empty string.
    item_empty = _make_github_item("", item_id="gh-empty")
    out_empty = enrich_items.build_item_content(item_empty)
    assert "【完整 README】" not in out_empty
    assert "owner/repo" in out_empty  # base content still rendered

    # Case B: detail_json present but no readme key.
    item_missing = _make_github_item(None, item_id="gh-missing")
    out_missing = enrich_items.build_item_content(item_missing)
    assert "【完整 README】" not in out_missing

    # Case C: malformed detail_json — must not raise.
    item_bad = {
        "id": "gh-bad",
        "platform": "github",
        "source": "trending_zh",
        "author_name": "owner",
        "title": "owner/repo",
        "content": "",
        "url": "https://github.com/owner/repo",
        "metrics_json": None,
        "detail_json": "{not valid json",
        "asr_text": "",
    }
    out_bad = enrich_items.build_item_content(item_bad)
    assert "【完整 README】" not in out_bad

    # Case D: detail_json missing entirely.
    item_no_detail = {
        "id": "gh-nodetail",
        "platform": "github",
        "source": "trending_zh",
        "author_name": "owner",
        "title": "owner/repo",
        "content": "",
        "url": "https://github.com/owner/repo",
        "metrics_json": None,
        "detail_json": None,
        "asr_text": "",
    }
    out_no = enrich_items.build_item_content(item_no_detail)
    assert "【完整 README】" not in out_no
