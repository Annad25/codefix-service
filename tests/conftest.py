"""Shared fixtures.

Parity tests import the grader itself (harness/grade.py) from the challenge
repository, found via CHALLENGE_DIR or next to this service. Only the tasks the
challenge README lists as public are used.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import shutil
import sys
import tarfile
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.config import Settings  # noqa: E402
from app.deadline import Deadline  # noqa: E402
from app.profiler import profile_repo  # noqa: E402
from app.sandbox import LocalSandbox  # noqa: E402
from app.tools import PathJail, ToolContext, default_registry  # noqa: E402
from app.workspace import Workspace  # noqa: E402

CHALLENGE_DIR = Path(os.environ.get("CHALLENGE_DIR", SERVICE_ROOT.parent / "Challenge-main"))
PUBLIC_TASKS = ["01-pricing-tax", "02-slugify", "03-token-bucket", "06-js-parse-duration",
                "07-bash-semver-bump", "08-go-ring-buffer", "11-c-str-trim"]

HAS_NODE = shutil.which("node") is not None


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def grade():
    path = CHALLENGE_DIR / "harness" / "grade.py"
    if not path.exists():
        pytest.skip(f"challenge repository not found at {CHALLENGE_DIR}")
    spec = importlib.util.spec_from_file_location("grade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def task_dir(name: str) -> Path:
    return CHALLENGE_DIR / "tasks" / "public" / name


@pytest.fixture(scope="session")
def task_archive(grade):
    cache: dict[str, bytes] = {}

    def get(name: str) -> bytes:
        if name not in cache:
            cache[name] = grade.pack_repo(task_dir(name))
        return cache[name]
    return get


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(sandbox_backend="local", work_root=tmp_path / "work")


@pytest.fixture
def sandbox() -> LocalSandbox:
    return LocalSandbox()


@pytest.fixture
def make_ctx(task_archive, settings, sandbox, tmp_path):
    """Build a ToolContext on a fresh worktree of a public task."""
    workspaces: list[Workspace] = []

    def make(name: str = "01-pricing-tax", *, deadline_s: float = 600, derived=None,
             settings_override: Settings | None = None) -> ToolContext:
        ws = Workspace.create(task_archive(name), settings.work_root, name)
        workspaces.append(ws)
        wt = ws.new_worktree("a")
        profile = profile_repo(ws.pristine)
        return ToolContext(
            jail=PathJail(wt, frozenset(profile.test_files)),
            sandbox=sandbox, settings=settings_override or settings,
            deadline=Deadline.after(deadline_s), scratch_parent=ws.scratch_dir(),
            test_command=profile.test_command, derived=derived,
        )
    yield make
    for ws in workspaces:
        ws.cleanup()


@pytest.fixture
def registry():
    return default_registry()


def sse_chunks(completion: dict) -> list[bytes]:
    """Turn a chat.completion dict into the SSE events OpenRouter streams for it."""
    import json as _json
    base = {"id": completion.get("id", "gen"), "object": "chat.completion.chunk", "created": 1,
            "model": completion.get("model", "m")}
    events = []
    for i, choice in enumerate(completion.get("choices", [])):
        msg = choice["message"]
        delta = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            delta["tool_calls"] = [{"index": j, **tc} for j, tc in enumerate(msg["tool_calls"])]
        if msg.get("reasoning_details"):
            delta["reasoning_details"] = msg["reasoning_details"]
        events.append({**base, "choices": [{"index": i, "delta": delta, "finish_reason": None}]})
        events.append({**base, "choices": [{"index": i, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]})
    if "usage" in completion:
        events.append({**base, "choices": [], "usage": completion["usage"]})
    return [f"data: {_json.dumps(e)}\n\n".encode() for e in events] + [b"data: [DONE]\n\n"]


def sse_response(completion: dict):
    import httpx as _httpx
    return _httpx.Response(200, content=b"".join(sse_chunks(completion)),
                           headers={"content-type": "text/event-stream"})


def make_tar(entries: list[tarfile.TarInfo | tuple[str, bytes]]) -> bytes:
    """Build a tar.gz from (name, content) pairs or raw TarInfo objects."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for entry in entries:
            if isinstance(entry, tarfile.TarInfo):
                tar.addfile(entry, io.BytesIO(b"") if entry.isfile() else None)
            else:
                name, data = entry
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# Hand-written fixes for public tasks, used to prove the pipeline end to end.
FIX_01 = """diff --git a/src/pricing/cart.py b/src/pricing/cart.py
--- a/src/pricing/cart.py
+++ b/src/pricing/cart.py
@@ -13,4 +13,4 @@ def subtotal(items):


 def total(items, tax_rate):
-    return round(subtotal(items) * tax_rate, 2)
+    return round(subtotal(items) * (1 + tax_rate), 2)
"""

BUMP_07 = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then
  echo "usage: bump_version.sh <version> <major|minor|patch>" >&2
  exit 2
fi
version="$1"
part="$2"
prefix=""
if [[ "$version" == v* ]]; then
  prefix="v"
  version="${version#v}"
fi
if [[ ! "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "invalid version: $1" >&2
  exit 2
fi
major=$((10#${BASH_REMATCH[1]}))
minor=$((10#${BASH_REMATCH[2]}))
patch=$((10#${BASH_REMATCH[3]}))
case "$part" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "unknown part: $part" >&2; exit 2 ;;
esac
echo "${prefix}${major}.${minor}.${patch}"
"""

PARSE_06 = r"""const UNITS = { d: 86400, h: 3600, m: 60, s: 1 };
const ORDER = "dhms";

module.exports = function parseDuration(text) {
  if (typeof text !== "string") throw new TypeError("duration must be a string");
  const src = text.trim();
  if (src === "") throw new TypeError("empty duration");
  const re = /(\d+)([a-zA-Z])\s*/y;
  let pos = 0;
  let last = -1;
  let total = 0;
  while (pos < src.length) {
    re.lastIndex = pos;
    const m = re.exec(src);
    if (!m) throw new TypeError(`invalid duration: ${text}`);
    const unit = m[2].toLowerCase();
    const idx = ORDER.indexOf(unit);
    if (idx === -1) throw new TypeError(`unknown unit: ${m[2]}`);
    if (idx <= last) throw new TypeError("units out of order or repeated");
    last = idx;
    total += Number(m[1]) * UNITS[unit];
    pos = re.lastIndex;
  }
  return total;
};
"""
