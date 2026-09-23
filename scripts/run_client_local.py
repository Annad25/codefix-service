"""Run the challenge's harness/run_client.py on a Windows dev machine.

Identical to `python harness/run_client.py ...` except that the grader's plain
"bash" calls are pointed at Git Bash (on Windows, "bash" can resolve to the WSL
launcher). Use it with --local only for development; graded runs use Docker.

    python scripts/run_client_local.py --url http://localhost:8765 --local <task dirs...>
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import find_bash  # noqa: E402

CHALLENGE = Path(os.environ.get("CHALLENGE_DIR", ROOT.parent / "Challenge-main"))
sys.path.insert(0, str(CHALLENGE / "harness"))

bash, real_run = find_bash(), subprocess.run


def run_with_real_bash(argv, *a, **kw):
    if isinstance(argv, list) and argv and argv[0] == "bash":
        argv = [bash, *argv[1:]]
    return real_run(argv, *a, **kw)


subprocess.run = run_with_real_bash

# run_client.py saves the diff with Path.write_text(), which on Windows turns
# "\n" into "\r\n" and makes the grader reject it for CRLF. Force LF, as on Linux.
_write_text = Path.write_text


def write_text_lf(self, data, encoding=None, errors=None, newline=None):
    return _write_text(self, data, encoding=encoding, errors=errors, newline="\n" if newline is None else newline)


Path.write_text = write_text_lf
spec = importlib.util.spec_from_file_location("run_client", CHALLENGE / "harness" / "run_client.py")
run_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_client)
sys.exit(run_client.main())
