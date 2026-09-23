"""End-to-end demo of the tool layer without an LLM.

A scripted "agent" drives the real tools on public tasks, exactly as the model
will: explore, run tests (fail), edit, run tests (pass), submit. Then the
canonical diff is built, sent through our delivery gate, and finally graded by
the challenge's own harness/grade.py (local mode, public tasks only).

    python scripts/demo_tools.py            # uses ../Challenge-main
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings, find_bash  # noqa: E402
from app.deadline import Deadline  # noqa: E402
from app.diffing import build_diff  # noqa: E402
from app.gate import run_gate  # noqa: E402
from app.profiler import profile_repo  # noqa: E402
from app.sandbox import make_sandbox  # noqa: E402
from app.tools import PathJail, ToolContext, default_registry  # noqa: E402
from app.workspace import Workspace  # noqa: E402

CHALLENGE = Path(os.environ.get("CHALLENGE_DIR", ROOT.parent / "Challenge-main"))

BUMP = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 ]]; then echo "usage: bump_version.sh <version> <major|minor|patch>" >&2; exit 2; fi
version="$1"; part="$2"; prefix=""
if [[ "$version" == v* ]]; then prefix="v"; version="${version#v}"; fi
if [[ ! "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then echo "invalid version: $1" >&2; exit 2; fi
major=$((10#${BASH_REMATCH[1]})); minor=$((10#${BASH_REMATCH[2]})); patch=$((10#${BASH_REMATCH[3]}))
case "$part" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
  *) echo "unknown part: $part" >&2; exit 2 ;;
esac
echo "${prefix}${major}.${minor}.${patch}"
"""

PARSE = r"""const UNITS = { d: 86400, h: 3600, m: 60, s: 1 };
const ORDER = "dhms";

module.exports = function parseDuration(text) {
  if (typeof text !== "string") throw new TypeError("duration must be a string");
  const src = text.trim();
  if (src === "") throw new TypeError("empty duration");
  const re = /(\d+)([a-zA-Z])\s*/y;
  let pos = 0, last = -1, total = 0;
  while (pos < src.length) {
    re.lastIndex = pos;
    const m = re.exec(src);
    if (!m) throw new TypeError(`invalid duration: ${text}`);
    const idx = ORDER.indexOf(m[2].toLowerCase());
    if (idx === -1) throw new TypeError(`unknown unit: ${m[2]}`);
    if (idx <= last) throw new TypeError("units out of order or repeated");
    last = idx;
    total += Number(m[1]) * UNITS[ORDER[idx]];
    pos = re.lastIndex;
  }
  return total;
};
"""

# Scripted tool calls per task: what a model would plausibly do.
SCRIPTS = {
    "01-pricing-tax": [
        ("list_dir", {"path": "."}),
        ("search", {"pattern": r"def total", "path": None}),
        ("read_file", {"path": "src/pricing/cart.py", "start_line": None, "end_line": None}),
        ("run_tests", {}),
        ("str_replace", {"path": "tests/test_cart.py", "old_str": "12.0", "new_str": "2.0"}),   # must be refused
        ("str_replace", {"path": "src/pricing/cart.py", "old_str": "* tax_rate, 2)",
                         "new_str": "* (1 + tax_rate), 2)"}),
        ("run_tests", {}),
        ("submit", {"summary": "total() now returns subtotal plus tax, rounded to 2 decimals."}),
    ],
    "07-bash-semver-bump": [
        ("read_file", {"path": "bin/bump_version.sh", "start_line": None, "end_line": None}),
        ("run_tests", {}),
        ("write_file", {"path": "bin/bump_version.sh", "content": BUMP}),
        ("run_command", {"command": "bash bin/bump_version.sh v1.2.3 patch; bash bin/bump_version.sh 1.2 minor; echo exit=$?"}),
        ("run_tests", {}),
        ("submit", {"summary": "Reset lower parts, keep optional v prefix, validate input with exit 2."}),
    ],
    "06-js-parse-duration": [
        ("read_file", {"path": "src/parseDuration.js", "start_line": None, "end_line": None}),
        ("read_file", {"path": "../../etc/passwd", "start_line": None, "end_line": None}),   # must be refused
        ("run_tests", {}),
        ("write_file", {"path": "src/parseDuration.js", "content": PARSE}),
        ("run_command", {"command": "node -e \"const p=require('./src/parseDuration');console.log(p('2d 4h'), p('1H 5M 3S'))\""}),
        ("run_tests", {}),
        ("submit", {"summary": "Implemented ordered d/h/m/s parser that throws TypeError on bad input."}),
    ],
}


def load_grade():
    spec = importlib.util.spec_from_file_location("grade", CHALLENGE / "harness" / "grade.py")
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    bash, real_run = find_bash(), subprocess.run

    def run_with_real_bash(argv, *a, **kw):        # on Windows "bash" may resolve to WSL
        if isinstance(argv, list) and argv and argv[0] == "bash":
            argv = [bash, *argv[1:]]
        return real_run(argv, *a, **kw)
    grade.subprocess.run = run_with_real_bash
    return grade


def short(text: str, n: int = 6) -> str:
    lines = text.strip().splitlines()
    body = "\n".join("      " + ln for ln in lines[:n])
    return body + (f"\n      ... ({len(lines) - n} more lines)" if len(lines) > n else "")


async def demo(task: str, grade, settings: Settings) -> dict:
    print(f"\n{'=' * 78}\nTASK {task}\n{'=' * 78}")
    t0 = time.monotonic()
    archive = grade.pack_repo(CHALLENGE / "tasks" / "public" / task)
    ws = Workspace.create(archive, settings.work_root, task)
    try:
        profile = profile_repo(ws.pristine)
        print(f"profile: language={profile.language}  test_command={profile.test_command!r}  "
              f"protected tests={len(profile.test_files)}")
        ctx = ToolContext(jail=PathJail(ws.new_worktree("a"), frozenset(profile.test_files)),
                          sandbox=make_sandbox(settings), settings=settings,
                          deadline=Deadline.after(180), scratch_parent=ws.scratch_dir(),
                          test_command=profile.test_command)
        registry = default_registry()
        for name, args in SCRIPTS[task]:
            result = await registry.dispatch(ctx, name, args)
            flag = "ERROR" if result.is_error else "ok   "
            shown = {k: (v[:40] + "...") if isinstance(v, str) and len(v) > 40 else v for k, v in args.items()}
            print(f"  [{flag}] {name}({json.dumps(shown)})")
            print(short(result.output))

        built = await build_diff(ctx.root)
        print(f"\ndiff: {len(built.diff)} bytes, files={built.files}")
        gate = await run_gate(archive, built.diff, test_command=profile.test_command,
                              sandbox=ctx.sandbox, scratch_parent=ws.scratch_dir(), timeout=120,
                              protected_paths=profile.test_files)
        print(f"our gate: tier={gate.tier.value} stage={gate.stage} ({gate.duration_s:.1f}s)")

        diff_path = Path(tempfile.mkdtemp()) / f"{task}.diff"
        diff_path.write_bytes(built.diff.encode())
        verdict = grade.grade(CHALLENGE / "tasks" / "public" / task, diff_path, True, "unused")
        print(f"grade.py (public acceptance tests, --local): {verdict['verdict']} at stage {verdict['stage']}")
        return {"task": task, "gate_tier": gate.tier.value, "grader": verdict["verdict"],
                "seconds": round(time.monotonic() - t0, 1)}
    finally:
        ws.cleanup()


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # test runners print unicode marks
    grade = load_grade()
    settings = Settings(sandbox_backend=os.environ.get("CODEFIX_SANDBOX", "local"),
                        work_root=ROOT / ".t" / "demo")
    print(f"sandbox backend: {settings.sandbox_backend}"
          + ("  (development only; real runs must use docker)" if settings.sandbox_backend == "local" else ""))
    rows = [await demo(task, grade, settings) for task in SCRIPTS]
    print(f"\n{'=' * 78}\nSUMMARY")
    for r in rows:
        print(f"  {r['task']:<24} gate tier {r['gate_tier']}   grader: {r['grader']:<7} {r['seconds']}s")
    return 0 if all(r["gate_tier"] == "A" and r["grader"] == "accept" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
