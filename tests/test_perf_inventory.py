import importlib.util
import os
import sys

_SPEC = importlib.util.spec_from_file_location(
    "perf_inventory",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "perf_inventory.py"),
)
pi = importlib.util.module_from_spec(_SPEC)
sys.modules["perf_inventory"] = pi
_SPEC.loader.exec_module(pi)


def test_extract_env_knobs_with_default():
    files = {
        "src/x.py": (
            'FOO_ENV = "INFO2ACTION_FOO_SEC"\n'
            "def _foo():\n"
            "    return _env_int(env, FOO_ENV, 42, min_value=0)\n"
            'BAR_ENV = "INFO2ACTION_BAR"\n'
        )
    }
    rows = pi.extract_env_knobs(files)
    by_env = {r["env"]: r for r in rows}
    assert by_env["INFO2ACTION_FOO_SEC"]["default"] == "42"
    assert "INFO2ACTION_BAR" in by_env  # 无默认也要列出


def test_extract_env_knobs_dedupes_same_env():
    files = {
        "a.py": 'A_ENV = "INFO2ACTION_DUP"\n',
        "b.py": 'B_ENV = "INFO2ACTION_DUP"\n',
    }
    rows = pi.extract_env_knobs(files)
    assert sum(1 for r in rows if r["env"] == "INFO2ACTION_DUP") == 1


def test_extract_statement_timeouts_attributes_function():
    files = {
        "src/y.py": (
            "def my_query(conn):\n"
            "    _set_short_statement_timeout(conn, 6000)\n"
        )
    }
    rows = pi.extract_statement_timeouts(files)
    assert rows and rows[0]["expr"] == "6000" and rows[0]["fn"] == "my_query"


def test_replace_block_is_idempotent_and_scoped():
    doc = (
        "# doc\n\n前言(手写,勿动)\n\n"
        f"{pi.MARK_START} old -->\n旧内容\n{pi.MARK_END}\n\n"
        "## 5. 后文(手写,勿动)\n"
    )
    new_block = f"{pi.MARK_START} new -->\n新内容\n{pi.MARK_END}"
    out1 = pi.replace_block(doc, new_block)
    assert "前言(手写,勿动)" in out1 and "## 5. 后文(手写,勿动)" in out1
    assert "旧内容" not in out1 and "新内容" in out1
    # 幂等:再替换一次同样的块,结果一致
    out2 = pi.replace_block(out1, new_block)
    assert out1 == out2


def test_replace_block_returns_none_without_markers():
    assert pi.replace_block("# doc without markers", "x") is None
