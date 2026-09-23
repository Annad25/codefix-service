"""Correctness evaluation: solve each task through the running service, grade with harness/grade.py.

Unlike harness/run_client.py this can retain diagnostics from an interrupted
request, but it never extends the service's client deadline. It reports
correctness and deadline compliance separately.
Grading uses the acceptance image with networking disabled, exactly as the
grader does (pass --local only if Docker is unavailable).

    python scripts/eval_tasks.py --url http://127.0.0.1:8766 ../Challenge-main/tasks/public/*
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import find_bash  # noqa: E402

CHALLENGE = Path(os.environ.get("CHALLENGE_DIR", ROOT.parent / "Challenge-main"))
# Git for Windows' system config sets core.autocrlf=true, which would make the
# grader's `git apply` write CRLF files on this host. Linux graders do not.
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"


def load_grade():
    spec = importlib.util.spec_from_file_location("grade", CHALLENGE / "harness" / "grade.py")
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    bash, real_run = find_bash(), subprocess.run

    def run_with_real_bash(argv, *a, **kw):        # --local on Windows: "bash" may be the WSL launcher
        if isinstance(argv, list) and argv and argv[0] == "bash":
            argv = [bash, *argv[1:]]
        return real_run(argv, *a, **kw)
    grade.subprocess.run = run_with_real_bash
    return grade


def solve(url: str, task_dir: Path, grade, timeout: float) -> tuple[dict, float]:
    meta = json.loads((task_dir / "task.json").read_text())
    payload = {"request_id": meta["request_id"], "task": meta["task"],
               "deadline_seconds": meta["deadline_seconds"],
               "repo_archive_b64": base64.b64encode(grade.pack_repo(task_dir)).decode()}
    req = urllib.request.Request(f"{url}/solve", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    return body, time.monotonic() - started


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("tasks", nargs="+", type=Path)
    p.add_argument("--url", required=True)
    p.add_argument("--local", action="store_true", help="grade on the host instead of the acceptance image")
    p.add_argument("--image", default="acceptance:latest")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--out", type=Path, default=ROOT / "results" / "eval")
    a = p.parse_args()
    grade = load_grade()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for task_dir in a.tasks:
        meta = json.loads((task_dir / "task.json").read_text())
        row = {"task": task_dir.name, "deadline_s": meta["deadline_seconds"]}
        try:
            body, elapsed = solve(a.url, task_dir, grade, a.timeout)
        except Exception as exc:
            row.update(outcome="no_response", reason=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(json.dumps(row))
            continue
        (a.out / f"{task_dir.name}.response.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
        usage = body.get("usage") or {}
        row.update(elapsed_s=round(elapsed, 1), within_deadline=elapsed <= meta["deadline_seconds"],
                   calls=usage.get("calls"), cost_usd=usage.get("estimated_cost_usd"))
        if not body.get("diff"):
            row.update(outcome="null_diff")
        else:
            diff_path = a.out / f"{task_dir.name}.diff"
            diff_path.write_bytes(body["diff"].encode("utf-8"))
            verdict = grade.grade(task_dir, diff_path, a.local, a.image)
            row.update(outcome=verdict["verdict"], stage=verdict["stage"],
                       reason=(verdict["reason"] or "")[-600:] or None)
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "reason"}), flush=True)

    n = len(rows)
    correct = sum(r["outcome"] == "accept" for r in rows)
    on_time = sum(r["outcome"] == "accept" and r.get("within_deadline") for r in rows)
    summary = {"tasks": n, "correct": correct, "correct_and_on_time": on_time,
               "null_diff": sum(r["outcome"] == "null_diff" for r in rows),
               "rejected": sum(r["outcome"] == "reject" for r in rows),
               "mean_elapsed_s": round(sum(r.get("elapsed_s", 0) for r in rows) / n, 1) if n else 0,
               "total_cost_usd": round(sum(r.get("cost_usd") or 0 for r in rows), 6),
               "grading": "local" if a.local else f"docker ({a.image}, --network none)"}
    report = {"summary": summary, "tasks": rows}
    (a.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in rows:
        if r.get("reason") and r["outcome"] != "accept":
            print(f"\n--- {r['task']} ({r['outcome']}, stage {r.get('stage')}):\n{r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
