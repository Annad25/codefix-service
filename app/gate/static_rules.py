"""Verbatim port of static_check() from harness/grade.py (public rules 1-8).

Do not "improve" this function: its only job is to agree with the grader.
tests/test_static_rules_parity.py imports the original and checks both
functions give the same answer on the same inputs.
"""
from __future__ import annotations

import re

MAX_DIFF_BYTES = 200_000
FORBIDDEN_PATH_PREFIXES = (".git/", "acceptance/", "acceptance_tests/")


def static_check(diff_bytes: bytes) -> str | None:
    """Return a rejection reason, or None if the diff passes the public rules."""
    if len(diff_bytes) == 0:
        return "empty diff"
    if len(diff_bytes) > MAX_DIFF_BYTES:
        return f"diff is {len(diff_bytes)} bytes; limit is {MAX_DIFF_BYTES}"
    try:
        text = diff_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "diff is not valid UTF-8"
    if "\r\n" in text:
        return "CRLF line endings"
    if "GIT binary patch" in text:
        return "binary patch content"
    if not text.startswith("diff --git "):
        return "not a git unified diff (must start with 'diff --git ')"
    for line in text.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        if not m:
            continue
        for path in m.groups():
            if path.startswith("/") or ".." in path.split("/"):
                return f"path escapes repository: {path}"
            if path.startswith(FORBIDDEN_PATH_PREFIXES):
                return f"path is restricted: {path}"
        if line.startswith("new file mode 120000") or line.startswith("new mode 120000"):
            return "symlink creation is not allowed"
    return None
