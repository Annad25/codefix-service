"""The diff builder's output always passes the grader's checks and round-trips exactly."""
from __future__ import annotations

import pytest

from app.diffing import DiffError, build_diff, changed_paths, is_artifact, normalize
from app.workspace import Workspace

from conftest import run, task_dir


def test_round_trip_through_the_graders_own_apply(grade, task_archive, tmp_path):
    ws = Workspace.create(task_archive("01-pricing-tax"), tmp_path / "w", "rt")
    wt = ws.new_worktree("a")
    cart = wt / "src/pricing/cart.py"
    cart.write_bytes(cart.read_bytes().replace(b"* tax_rate, 2)", b"* (1 + tax_rate), 2)"))
    (wt / "src/pricing/helpers.py").write_bytes(b"def half(x):\n    return x / 2\n")
    (wt / "README.md").unlink()
    (wt / "build").mkdir()
    (wt / "build/out.o").write_bytes(b"\x00junk")
    (wt / "src/pricing/__pycache__").mkdir()
    (wt / "src/pricing/__pycache__/cart.cpython-312.pyc").write_bytes(b"\x00")

    built = run(build_diff(wt))
    assert set(built.excluded) == {"build/out.o", "src/pricing/__pycache__/cart.cpython-312.pyc"}
    assert set(changed_paths(built.diff)) == {"README.md", "src/pricing/cart.py", "src/pricing/helpers.py"}
    assert built.diff.endswith("\n") and "old mode" not in built.diff

    diff_bytes = built.diff.encode()
    assert grade.static_check(diff_bytes) is None
    fresh = grade.materialize_repo(task_dir("01-pricing-tax") / "repo", tmp_path / "fresh")
    patch = tmp_path / "c.diff"
    patch.write_bytes(diff_bytes)
    assert grade.apply_diff(fresh, patch) is None
    # grade.py calls plain `git apply`; Git for Windows' system config sets
    # core.autocrlf=true, which checks files out with CRLF on a Windows host.
    # Linux graders do not do this, so compare with line endings normalised.
    for rel in ("src/pricing/cart.py", "src/pricing/helpers.py"):
        assert (fresh / rel).read_bytes().replace(b"\r\n", b"\n") == (wt / rel).read_bytes()
    assert not (fresh / "README.md").exists()
    ws.cleanup()


def test_no_changes_gives_empty_diff(task_archive, tmp_path):
    ws = Workspace.create(task_archive("01-pricing-tax"), tmp_path / "w", "empty")
    assert run(build_diff(ws.new_worktree("a"))).diff == ""
    ws.cleanup()


def test_binary_change_is_refused(task_archive, tmp_path):
    ws = Workspace.create(task_archive("01-pricing-tax"), tmp_path / "w", "bin")
    wt = ws.new_worktree("a")
    (wt / "data.dat").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(DiffError, match="binary"):
        run(build_diff(wt))
    ws.cleanup()


def test_normalize_strips_mode_only_blocks_and_adds_newline():
    raw = ("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
           "diff --git a/x.py b/x.py\nold mode 100644\nnew mode 100755\nindex 1..2\n--- a/x.py\n+++ b/x.py\n"
           "@@ -1 +1 @@\n-a\n+b")
    out = normalize(raw)
    assert out.startswith("diff --git a/x.py b/x.py\nindex 1..2\n")
    assert "mode" not in out and out.endswith("+b\n")


def test_artifact_patterns():
    assert is_artifact("target/debug/x") and is_artifact("a/__pycache__/b.pyc") and is_artifact("Cargo.lock")
    assert is_artifact("out/roman/RomanNumerals.class")
    assert not is_artifact("src/build_helpers.py") and not is_artifact("src/output.c")
