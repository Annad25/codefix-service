"""Minimal .env loader (no dependency): KEY=VALUE lines, # comments, optional quotes.

Values already present in the process environment win, so a deployment can
always override the file.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path) -> list[str]:
    """Load path into os.environ without overriding existing keys. Returns keys set."""
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
