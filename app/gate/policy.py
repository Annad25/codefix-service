"""Our own delivery rules, applied after the grader's static rules.

These are stricter than the grader, never looser. They close gaps in the
reference check (its symlink test never runs because of an early `continue`,
and headers with spaces in paths skip its path checks) and enforce the
service's own policy: existing tests are not edited, reserved names are not
created, and diffs are in the canonical form the diff builder produces.

Only the header section of each file block (everything before the first
"@@" hunk) is inspected, so file content can never be mistaken for metadata.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterable

FORBIDDEN_PREFIXES = (".git/", "acceptance/", "acceptance_tests/")
RESERVED_BASENAME_PREFIXES = ("zz_acceptance", "zz_derived")
_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _path_problem(path: str) -> str | None:
    if path == "/dev/null":
        return None
    p = PurePosixPath(path)
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or "\\" in path:
        return f"path is not a relative POSIX path: {path}"
    if ".." in p.parts:
        return f"path escapes repository: {path}"
    if path.startswith(FORBIDDEN_PREFIXES) or p.parts[0] == ".git":
        return f"path is restricted: {path}"
    if p.name.startswith(RESERVED_BASENAME_PREFIXES):
        return f"path uses a reserved name: {path}"
    return None


def _file_line_path(line: str) -> str:
    """Path from a '--- a/x' or '+++ b/x' line."""
    path = line[4:].rstrip("\n").split("\t", 1)[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _header_sections(diff: str) -> list[list[str]]:
    sections: list[list[str]] = []
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            sections.append([line])
            in_header = True
        elif line.startswith("@@"):
            in_header = False
        elif in_header:
            sections[-1].append(line)
    return sections


def policy_violations(diff: str, protected_paths: Iterable[str] = ()) -> list[str]:
    """Return every rule the diff breaks (empty list = compliant)."""
    problems: list[str] = []
    protected = set(protected_paths)
    if not diff.startswith("diff --git "):
        problems.append("diff must start with 'diff --git '")
    if not diff.endswith("\n"):
        problems.append("diff does not end with a newline")
    touched: list[str] = []
    for section in _header_sections(diff):
        m = _HEADER.match(section[0])
        if not m:
            problems.append(f"unparseable diff header: {section[0]}")
        else:
            touched += [m.group(1), m.group(2)]
        for line in section[1:]:
            if line.startswith(("--- ", "+++ ")):
                touched.append(_file_line_path(line))
            elif re.match(r"^(new file mode|new mode|old mode|deleted file mode) 120000", line):
                problems.append("symlinks are not allowed")
            elif line.startswith(("old mode ", "new mode ")):
                problems.append("file mode changes are not allowed")
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                problems.append("renames/copies must be expressed as delete + add")
            elif line.startswith(("GIT binary patch", "Binary files ")):
                problems.append("binary content is not allowed")
    for path in dict.fromkeys(touched):
        problem = _path_problem(path)
        if problem:
            problems.append(problem)
        elif path in protected:
            problems.append(f"existing test file must not be modified: {path}")
    return list(dict.fromkeys(problems))
