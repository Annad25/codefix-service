"""Path jail: every path the model names is resolved here, and nowhere else.

Rules, in order:
  - relative POSIX-style paths only (no absolute paths, drive letters or NUL)
  - no ".." components
  - nothing under .git, acceptance/ or acceptance_tests/
  - no symlink at any component (the snapshot never contains one)
  - the resolved path must stay inside the root

Writes additionally refuse pre-existing test files (the agent fixes code, not
tests) and reserved names used by the grader and by our derived checks.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BLOCKED_TOP = {".git", "acceptance", "acceptance_tests"}
RESERVED_BASENAME_PREFIXES = ("zz_acceptance", "zz_derived")


class JailError(Exception):
    pass


@dataclass
class PathJail:
    root: Path
    protected: frozenset[str] = field(default_factory=frozenset)   # repo-relative POSIX paths

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))

    def _parts(self, rel: str) -> tuple[str, ...]:
        if not isinstance(rel, str):
            raise JailError("path must be a string")
        raw = rel.strip()
        if "\x00" in raw:
            raise JailError("path contains a NUL byte")
        raw = raw.replace("\\", "/")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("~"):
            raise JailError(f"'{rel}' is absolute; use a path relative to the repository root")
        parts = tuple(p for p in PurePosixPath(raw).parts if p not in ("", "."))
        if ".." in parts:
            raise JailError(f"'{rel}' leaves the repository ('..' is not allowed)")
        if parts and (parts[0] in BLOCKED_TOP or ".git" in parts):
            raise JailError(f"'{rel}' is outside the area you may access")
        return parts

    def resolve(self, rel: str) -> tuple[Path, str]:
        """Return (absolute path, normalised repo-relative POSIX path) for a read."""
        parts = self._parts(rel)
        current = self.root
        for part in parts:
            current = current / part
            if os.path.islink(current):
                raise JailError(f"'{rel}' goes through a symbolic link, which is not allowed")
        real = Path(os.path.realpath(current))
        if real != self.root and self.root not in real.parents:
            raise JailError(f"'{rel}' resolves outside the repository")
        return current, "/".join(parts) or "."

    def resolve_for_write(self, rel: str) -> tuple[Path, str]:
        path, norm = self.resolve(rel)
        if norm == ".":
            raise JailError("cannot write to the repository root")
        if norm in self.protected:
            raise JailError(f"'{norm}' is an existing test file and is read-only; "
                            "change the source code instead")
        if PurePosixPath(norm).name.startswith(RESERVED_BASENAME_PREFIXES):
            raise JailError(f"'{norm}' uses a reserved file name")
        if path.exists() and path.is_dir():
            raise JailError(f"'{norm}' is a directory")
        return path, norm

    def relative(self, path: Path) -> str:
        return Path(path).relative_to(self.root).as_posix()
