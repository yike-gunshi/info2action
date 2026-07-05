"""Small `.env` reader for CLI/runtime config.

The project starts servers via shell scripts that source `.env`, but one-off
batch scripts are often run directly. This helper gives runtime config the same
secret source without adding a dependency on python-dotenv.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_project_env(base_dir: str | os.PathLike[str]) -> dict[str, str]:
    """Return key/value pairs from `<base_dir>/.env` without mutating environ."""
    env_path = Path(base_dir) / '.env'
    try:
        lines = env_path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values
