"""Request workspaces: safe extraction of the snapshot and isolated copies.

The archive is untrusted input. It is pre-scanned and anything that is not a
plain file or directory is rejected before extraction (a source snapshot never
needs links or devices, and link handling is where tarfile's own filters have
had bypasses). Extraction then also uses the PEP 706 "data" filter, and the
result is checked again for links.

Layout for one request:
    <work_root>/<request-dir>/pristine/repo   untouched snapshot, never modified
    <work_root>/<request-dir>/wt-<name>/      one working copy per candidate
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
import shutil
import stat
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 20_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
# The API enforces this before it calls decode_archive.  Keep the decoder safe
# when used directly too: base64 decoding an unbounded request would otherwise
# allocate before archive validation can reject it.
MAX_ARCHIVE_B64_CHARS = 16 * 1024 * 1024


class UnsafeArchiveError(ValueError):
    """The archive is malformed or contains something a source snapshot never should."""


def decode_archive(b64: str) -> bytes:
    if len(b64) > MAX_ARCHIVE_B64_CHARS:
        raise UnsafeArchiveError(
            f"repo_archive_b64 exceeds the {MAX_ARCHIVE_B64_CHARS}-character limit")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnsafeArchiveError(f"repo_archive_b64 is not valid base64: {exc}") from exc


def _check_member_name(name: str) -> None:
    if not name or "\\" in name or "\x00" in name:
        raise UnsafeArchiveError(f"invalid member name: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
    if ".." in PurePosixPath(name).parts:
        raise UnsafeArchiveError(f"path traversal in archive: {name!r}")


def _prescan(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise UnsafeArchiveError(f"archive has {len(members)} members; limit is {MAX_MEMBERS}")
    total = 0
    for m in members:
        _check_member_name(m.name)
        if not (m.isfile() or m.isdir()):
            kind = "symlink" if m.issym() else "hardlink" if m.islnk() else "special file"
            raise UnsafeArchiveError(f"{kind} in archive is not allowed: {m.name!r}")
        total += m.size
        if total > MAX_TOTAL_BYTES:
            raise UnsafeArchiveError("archive expands beyond the size limit")
    return members


def _assert_no_links(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, name)):
                raise UnsafeArchiveError(f"link found after extraction: {name}")


def _find_repo_root(dest: Path) -> Path:
    """The harness packs the snapshot rooted at repo/; accept a bare repo too."""
    if (dest / "repo" / ".git").is_dir():
        return dest / "repo"
    if (dest / ".git").is_dir():
        return dest
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) == 1 and (tops[0] / ".git").is_dir():
        return tops[0]
    raise UnsafeArchiveError("archive does not contain a git repository")


def safe_extract(archive: bytes, dest: Path) -> Path:
    """Extract a tar(.gz) snapshot into dest and return the repository root."""
    dest.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
            members = _prescan(tar)
            tar.extractall(dest, members=members, filter="data")
    except tarfile.TarError as exc:
        raise UnsafeArchiveError(f"cannot read archive: {exc}") from exc
    _assert_no_links(dest)
    return _find_repo_root(dest)


def _make_writable_and_retry(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def rmtree_force(path: Path, attempts: int = 10) -> None:
    """rmtree that copes with read-only files (git objects on Windows) and with
    Windows keeping a directory locked for a moment after a killed process exits."""
    for attempt in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onexc=_make_writable_and_retry)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(0.2)


def copy_tree(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _safe_dirname(request_id: str) -> str:
    # Kept short: Windows paths are limited to 260 characters and .git/objects nests deep.
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", request_id)[:24] or "request"
    return f"{clean}-{uuid.uuid4().hex[:8]}"


@dataclass
class Workspace:
    root: Path          # per-request directory
    archive: bytes      # original request bytes; the gate re-extracts from these
    pristine: Path      # repository root of the untouched snapshot

    @classmethod
    def create(cls, archive: bytes, work_root: Path, request_id: str) -> "Workspace":
        work_root.mkdir(parents=True, exist_ok=True)
        root = work_root / _safe_dirname(request_id)
        try:
            pristine = safe_extract(archive, root / "pristine")
        except BaseException:
            rmtree_force(root)
            raise
        return cls(root=root, archive=archive, pristine=pristine)

    def new_worktree(self, name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
        return copy_tree(self.pristine, self.root / f"wt-{safe}")

    def scratch_dir(self) -> Path:
        path = self.root / "scratch"
        path.mkdir(exist_ok=True)
        return path

    def cleanup(self) -> None:
        """Best effort: a leftover directory must never turn a finished solve into a failure."""
        try:
            rmtree_force(self.root)
        except OSError as exc:
            logging.getLogger("codefix.workspace").warning("could not remove %s: %s", self.root, exc)
