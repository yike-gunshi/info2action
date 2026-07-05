import json
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import fetch_waytoagi  # noqa: E402


class FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_fetch_doc_markdown_uses_bot_by_default(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return FakeResult(stdout=json.dumps({"ok": True, "data": {"markdown": "hello"}}))

    monkeypatch.setattr(fetch_waytoagi.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_waytoagi, "LARK_DOC_IDENTITY", "bot")

    assert fetch_waytoagi.fetch_doc_markdown("https://example.test/wiki/abc") == "hello"
    assert calls[0][calls[0].index("--as") + 1] == "bot"


def test_fetch_doc_markdown_falls_back_to_bot_when_user_auth_missing(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        identity = args[args.index("--as") + 1]
        if identity == "user":
            return FakeResult(stdout=json.dumps({
                "ok": False,
                "error": {"message": "need_user_authorization"},
            }))
        return FakeResult(stdout=json.dumps({"ok": True, "data": {"markdown": "bot doc"}}))

    monkeypatch.setattr(fetch_waytoagi.subprocess, "run", fake_run)
    monkeypatch.setattr(fetch_waytoagi, "LARK_DOC_IDENTITY", "user")

    assert fetch_waytoagi.fetch_doc_markdown("https://example.test/wiki/abc") == "bot doc"
    assert [args[args.index("--as") + 1] for args in calls] == ["user", "bot"]
