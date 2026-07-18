import fetch_lingowhale as lw


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_normalize_current_feed_entry_uses_channel_metadata():
    entry = lw._normalize_entry(
        {
            "entry_id": "6a152228f25935f00418ed31",
            "channel": {
                "name": "赛博禅心-公众号",
                "surface_url": "https://example.com/cover.png",
            },
        }
    )

    assert entry["info_source"]["info_source_name"] == "赛博禅心"
    assert entry["surface_url"] == "https://example.com/cover.png"


def test_fetch_subscription_feed_scans_registered_priority_channels_only(monkeypatch):
    calls = []
    groups_info = [
        {
            "name": "每日查看",
            "channels": [
                {"channel_id": "other", "name": "其他-公众号"},
                {"channel_id": "target", "name": "赛博禅心-公众号"},
            ],
        }
    ]

    def fake_fetch(endpoint, channel_ids, label, timeout=30, since_ts=None):
        calls.append((endpoint, tuple(channel_ids), label, timeout))
        if endpoint == lw.FEED_ENDPOINTS[0] and channel_ids == ["target"]:
            return (
                [
                    lw._normalize_entry(
                        {
                            "entry_id": "new",
                            "title": "怎么知道 Agent 真干完活了？",
                            "pub_time": 2,
                            "channel": {"name": "赛博禅心-公众号"},
                        }
                    )
                ],
                1,
                "done",
            )
        if endpoint == lw.FEED_ENDPOINTS[1]:
            return (
                [
                    {
                        "entry_id": "old",
                        "title": "legacy",
                        "pub_time": 1,
                        "info_source": {"info_source_name": "legacy"},
                    }
                ],
                1,
                "done",
            )
        return ([], 1, "empty")

    monkeypatch.setenv("INFO2ACTION_LINGOWHALE_REGISTRY_ONLY", "0")
    monkeypatch.setattr(lw, "_priority_channel_ids", lambda: ["target"])
    monkeypatch.setattr(lw, "_registry_lingowhale_channel_map", lambda: {"target": 1})
    monkeypatch.setattr(lw, "_fetch_subscription_feed_from_endpoint", fake_fetch)
    monkeypatch.setattr(lw, "_record_lingowhale_result", lambda source_id, *, ok, error=None: None)
    monkeypatch.setattr(lw.time, "sleep", lambda _: None)

    entries = lw.fetch_subscription_feed(groups_info)

    assert [entry["entry_id"] for entry in entries] == ["new"]
    assert calls[0][0] == lw.FEED_ENDPOINTS[0]
    assert calls[0][1] == ("target",)
    assert all(call[0] == lw.FEED_ENDPOINTS[0] for call in calls)


def test_priority_channel_ids_are_included_even_when_group_snapshot_misses_them():
    assert lw._prioritize_channel_ids(["known"], ["target", "known"]) == [
        "target",
        "known",
    ]


def test_enrich_entries_fetches_detail_when_abstract_exists_but_content_missing(monkeypatch):
    calls = []

    def fake_fetch_detail(entry):
        calls.append(entry["entry_id"])
        enriched = dict(entry)
        enriched["content"] = "full text"
        return enriched, True

    monkeypatch.setattr(lw, "_fetch_detail", fake_fetch_detail)

    [entry] = lw.enrich_entries(
        [{"entry_id": "6a152228f25935f00418ed31", "abstract": "summary"}]
    )

    assert calls == ["6a152228f25935f00418ed31"]
    assert entry["content"] == "full text"


def test_enrich_entries_fetches_detail_for_short_preview_content(monkeypatch):
    calls = []

    def fake_fetch_detail(entry):
        calls.append(entry["entry_id"])
        enriched = dict(entry)
        enriched["content"] = "full article body with Agent Workspace and 设计审美 sections"
        return enriched, True

    monkeypatch.setattr(lw, "_fetch_detail", fake_fetch_detail)

    [entry] = lw.enrich_entries(
        [{
            "entry_id": "6a16c3a900a9858cce2aae40",
            "content": "Marvis 是少数几个让我重新兴奋起来的。\n与 Marvis �",
        }]
    )

    assert calls == ["6a16c3a900a9858cce2aae40"]
    assert "Agent Workspace" in entry["content"]


def test_enrich_entries_fetches_detail_even_when_content_exists(monkeypatch):
    calls = []

    def fake_fetch_detail(entry):
        calls.append(entry["entry_id"])
        enriched = dict(entry)
        enriched["content"] = "detail api full text"
        return enriched, True

    monkeypatch.setattr(lw, "_fetch_detail", fake_fetch_detail)

    content = "这是一篇已经完整的公众号正文。" * 20
    [entry] = lw.enrich_entries(
        [{"entry_id": "full", "content": content}]
    )

    assert calls == ["full"]
    assert entry["content"] == "detail api full text"


def test_normalize_detail_content_merges_inline_fragments():
    raw = (
        "与 Marvis 的缘分始于\n"
        "上\n"
        "周刷到的一篇\n"
        "推文\n"
        "。\n"
        "1、终端调度能力：\n"
        "夯\n"
        "基本电脑唤起，\n"
        "2\n"
        "0\n"
        "秒\n"
        "之内\n"
        "完成从语音落音到任务执行的完整闭环。\n"
        "【深色模式唤起】\n"
        "同样基于端侧能力，Marvis 实现了对本地资源的直接穿透\n"
        "：\n"
        "文档不传云端，直接读硬盘里的 PDF、Word、Excel；\n"
        "4、Agent Workspace：\n"
        "夯爆了\n"
        "整体体验下来，最喜欢的功能是办公室的交互设计。\n"
        "7、设计审美：\n"
        "拉➡️NPC\n"
        "首先\n"
        "试了\n"
        "无\n"
        "skill\n"
        "自然\n"
        "语言\n"
        "生成，审美有点拉。"
    )

    cleaned = lw._normalize_detail_content_text(raw)
    lines = cleaned.splitlines()

    assert "与 Marvis 的缘分始于上周刷到的一篇推文。" in cleaned
    assert "20秒之内完成从语音落音到任务执行的完整闭环。" in cleaned
    assert "同样基于端侧能力，Marvis 实现了对本地资源的直接穿透：文档不传云端" in cleaned
    assert "1、终端调度能力：" in lines
    assert "【深色模式唤起】" in lines
    assert "4、Agent Workspace：" in lines
    assert "7、设计审美：" in lines
    assert "拉➡️NPC 首先试了无 skill 自然语言生成，审美有点拉。" in cleaned
    assert "\n上\n" not in cleaned
    assert "\n推文\n" not in cleaned


def test_normalize_detail_content_keeps_normal_paragraphs():
    raw = "第一段已经是完整自然段。\n第二段也是完整自然段。\n第三段继续说明。"

    assert lw._normalize_detail_content_text(raw) == raw


def test_normalize_detail_content_joins_split_ascii_words():
    raw = (
        "友情提醒：小红书案例仅是我实测 M\n"
        "arvis 用，不构成操作攻略建议。\n"
        "同样适合 AI\n"
        "Agent 协同任务。\n"
        "它\n"
        "仍然\n"
        "需要\n"
        "本地权限\n"
        "。"
    )

    cleaned = lw._normalize_detail_content_text(raw)

    assert "Marvis 用" in cleaned
    assert "AI Agent 协同任务" in cleaned
    assert "M arvis" not in cleaned


def test_fetch_detail_stores_normalized_content(monkeypatch):
    raw_content = "与 Marvis 的缘分始于\n上\n周刷到的一篇\n推文\n。"
    body = (
        '{"data":{"resource":{'
        '"title":"Marvis",'
        f'"content":{lw.json.dumps(raw_content, ensure_ascii=False)},'
        '"abstract":"summary",'
        '"viewpoint":["point"]'
        '}}}'
    )

    monkeypatch.setattr(lw.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse(body))

    entry, ok = lw._fetch_detail({"entry_id": "demo"})

    assert ok is True
    assert entry["content"] == "与 Marvis 的缘分始于上周刷到的一篇推文。"
