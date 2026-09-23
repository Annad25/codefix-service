"""Untrusted archives: good snapshots extract, anything unusual is rejected."""
from __future__ import annotations

import base64
import tarfile

import pytest

import app.workspace as workspace

from app.workspace import UnsafeArchiveError, Workspace, decode_archive, safe_extract

from conftest import make_tar


def test_harness_snapshot_extracts_to_repo_root(task_archive, tmp_path):
    root = safe_extract(task_archive("01-pricing-tax"), tmp_path / "x")
    assert root.name == "repo"
    assert (root / ".git").is_dir() and (root / "src" / "pricing" / "cart.py").is_file()


def _link(name: str, target: str, kind) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.mark.parametrize("entries, message", [
    ([("repo/.git/HEAD", b"x"), _link("repo/evil", "/etc/passwd", tarfile.SYMTYPE)], "symlink"),
    ([("repo/.git/HEAD", b"x"), _link("repo/evil", "repo/.git/HEAD", tarfile.LNKTYPE)], "hardlink"),
    ([("/etc/evil", b"x")], "absolute"),
    ([("repo/../../evil", b"x")], "traversal"),
    ([("C:/evil", b"x")], "absolute"),
    ([("repo\\evil", b"x")], "invalid member name"),
])
def test_malicious_members_are_rejected(entries, message, tmp_path):
    with pytest.raises(UnsafeArchiveError, match=message):
        safe_extract(make_tar(entries), tmp_path / "x")
    assert not (tmp_path / "evil").exists()


def test_special_files_are_rejected(tmp_path):
    fifo = tarfile.TarInfo("repo/fifo")
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(UnsafeArchiveError, match="special file"):
        safe_extract(make_tar([("repo/.git/HEAD", b"x"), fifo]), tmp_path / "x")


def test_archive_without_git_repo_is_rejected(tmp_path):
    with pytest.raises(UnsafeArchiveError, match="git repository"):
        safe_extract(make_tar([("repo/a.py", b"x")]), tmp_path / "x")


def test_garbage_and_bad_base64(tmp_path):
    with pytest.raises(UnsafeArchiveError):
        safe_extract(b"definitely not a tar", tmp_path / "x")
    with pytest.raises(UnsafeArchiveError, match="base64"):
        decode_archive("not base64 !!")
    assert decode_archive(base64.b64encode(b"ok").decode()) == b"ok"


def test_oversized_base64_is_rejected_before_decoding(monkeypatch):
    monkeypatch.setattr(workspace, "MAX_ARCHIVE_B64_CHARS", 4)
    with pytest.raises(UnsafeArchiveError, match="exceeds"):
        decode_archive("AAAAA")


def test_workspace_lifecycle(task_archive, tmp_path):
    ws = Workspace.create(task_archive("01-pricing-tax"), tmp_path, "../../weird id")
    assert ws.root.parent == tmp_path                       # request id cannot steer the path
    wt = ws.new_worktree("a")
    (wt / "src" / "pricing" / "cart.py").write_text("changed")
    assert (ws.pristine / "src" / "pricing" / "cart.py").read_text() != "changed"
    ws.cleanup()                                            # copes with read-only git objects
    assert not ws.root.exists()


def test_failed_create_leaves_nothing_behind(tmp_path):
    with pytest.raises(UnsafeArchiveError):
        Workspace.create(b"junk", tmp_path, "r1")
    assert list(tmp_path.iterdir()) == []
