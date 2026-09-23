"""Turn a candidate worktree into the canonical diff that gets delivered.

The grader runs `git apply --check` on a fresh snapshot and applies the public
rules (see gate/static_rules.py). This module produces diffs that satisfy both
by construction: git-generated, a/ b/ prefixes, no renames, no mode changes,
no binary content, no build artifacts, trailing newline.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .procs import git

# Paths that are build or tool outputs, never source. Excluded even if the
# agent's tree somehow contains them (tests run in copies, so this is a backstop).
ARTIFACT_DIRS = {"build", "out", "target", "__pycache__", "node_modules", "dist", ".pytest_cache"}
ARTIFACT_GLOBS = ("*.pyc", "*.pyo", "*.class", "*.o", "*.obj", "*.a", "*.so", "*.exe", "*.dll",
                  "Cargo.lock", ".DS_Store")


class DiffError(RuntimeError):
    pass


@dataclass
class DiffBuild:
    diff: str                                   # "" when there are no changes
    files: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)


def is_artifact(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if any(p in ARTIFACT_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], g) for g in ARTIFACT_GLOBS)


def split_file_blocks(diff: str) -> list[str]:
    """Split a git diff into per-file blocks, each starting with 'diff --git '."""
    blocks: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") or not blocks:
            blocks.append([])
        blocks[-1].append(line)
    return ["".join(b) for b in blocks]


def normalize(diff: str) -> str:
    """Drop mode-only noise, reject binary content, guarantee LF and a trailing newline."""
    if not diff.strip():
        return ""
    out: list[str] = []
    for block in split_file_blocks(diff):
        if "GIT binary patch" in block or "\nBinary files " in block:
            header = block.splitlines()[0]
            raise DiffError(f"binary change cannot be delivered: {header}")
        lines = [ln for ln in block.splitlines(keepends=True)
                 if not (ln.startswith("old mode ") or ln.startswith("new mode "))]
        # A block that only changed a mode is now just its header line: drop it.
        if len(lines) <= 1:
            continue
        out.append("".join(lines))
    text = "".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


async def build_diff(worktree: Path) -> DiffBuild:
    """Stage everything except artifacts and return the canonical cached diff."""
    r = await git(["add", "-A"], worktree)
    if not r.ok:
        raise DiffError(f"git add failed: {r.output}")
    r = await git(["diff", "--cached", "--name-only", "-z", "--no-renames"], worktree)
    staged = [p for p in r.stdout.split("\x00") if p]
    excluded = [p for p in staged if is_artifact(p)]
    if excluded:
        r = await git(["reset", "-q", "--", *excluded], worktree)
        if not r.ok:
            raise DiffError(f"git reset failed: {r.output}")
    r = await git(["diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv",
                   "--no-renames", "--src-prefix=a/", "--dst-prefix=b/"], worktree)
    if not r.ok:
        raise DiffError(f"git diff failed: {r.output}")
    diff = normalize(r.stdout)
    files = [p for p in staged if p not in excluded]
    return DiffBuild(diff=diff, files=files, excluded=excluded)


def changed_paths(diff: str) -> list[str]:
    """Paths named in 'diff --git a/X b/Y' headers (both sides, deduplicated, in order)."""
    seen: dict[str, None] = {}
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            rest = line[len("diff --git "):]
            if rest.startswith("a/") and " b/" in rest:
                a, b = rest[2:].split(" b/", 1)
                seen.setdefault(a)
                seen.setdefault(b)
    return list(seen)
