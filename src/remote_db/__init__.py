"""remote_db 包：由原 src/remote_db.py 纯机械拆分而来（T4.1）。

各段文件共享本模块的单一命名空间（exec 组合），以保持
monkeypatch.setattr(remote_db, ...) 与模块级可变状态的原有语义。
段文件不是独立模块，禁止单独 import remote_db.feed 之类。
"""
from pathlib import Path as _SplitPath

_SEGMENTS = (
    "_core",
    "fetch_runs",
    "highlights",
    "clusters",
    "items_write",
    "actions",
    "sources",
    "users_admin",
    "stats_misc",
    "feed",
)


def _load_segments() -> None:
    pkg_dir = _SplitPath(__file__).resolve().parent
    for _name in _SEGMENTS:
        _path = pkg_dir / f"{_name}.py"
        _code = compile(_path.read_text(encoding="utf-8"), str(_path), "exec")
        exec(_code, globals())


_load_segments()
