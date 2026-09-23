"""Our policy is stricter than the grader and never looser."""
from __future__ import annotations

from app.gate.policy import policy_violations

from conftest import FIX_01


def test_clean_diff_passes():
    assert policy_violations(FIX_01) == []


def test_symlink_is_caught_even_though_the_grader_misses_it(grade):
    diff = ("diff --git a/link b/link\nnew file mode 120000\nindex 0000000..1111111\n"
            "--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc/passwd\n\\ No newline at end of file\n")
    assert grade.static_check(diff.encode()) is None          # the grader's gap
    assert "symlinks are not allowed" in policy_violations(diff)


def test_paths_with_spaces_are_still_checked(grade):
    diff = "diff --git a/acceptance/my test.py b/acceptance/my test.py\n--- a/acceptance/my test.py\n+++ b/acceptance/my test.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert grade.static_check(diff.encode()) is None          # regex \\S+ skips this header
    assert any("restricted" in p for p in policy_violations(diff))


def test_protected_test_file_edit_is_rejected():
    diff = FIX_01.replace("src/pricing/cart.py", "tests/test_cart.py")
    assert policy_violations(diff, {"tests/test_cart.py"}) == [
        "existing test file must not be modified: tests/test_cart.py"]


def test_reserved_names_mode_changes_renames_and_newline():
    reserved = FIX_01.replace("src/pricing/cart.py", "tests/zz_acceptance.rs")
    assert any("reserved" in p for p in policy_violations(reserved))
    mode = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
    assert "file mode changes are not allowed" in policy_violations(mode)
    rename = "diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
    assert "renames/copies must be expressed as delete + add" in policy_violations(rename)
    assert "diff does not end with a newline" in policy_violations(FIX_01.rstrip("\n"))


def test_hunk_content_is_never_read_as_metadata():
    # A removed line whose text is "-- acceptance/x" renders as "--- acceptance/x" inside a hunk.
    diff = ("diff --git a/notes.md b/notes.md\n--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1 @@\n"
            "--- acceptance/x\n old mode 100644\n")
    assert policy_violations(diff) == []
