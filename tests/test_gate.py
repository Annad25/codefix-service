"""The gate: correct fixes reach tier A, and every failure stops at the right stage.

Where possible each verdict is cross-checked against the grader itself
(harness/grade.py, run with --local semantics on public tasks only).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import find_bash
from app.gate import DerivedChecks, Tier, run_gate
from app.profiler import profile_repo
from app.workspace import safe_extract

from conftest import BUMP_07, FIX_01, HAS_NODE, PARSE_06, run, task_dir


@pytest.fixture
def grade_local(grade, monkeypatch, tmp_path):
    """grade.grade(..., local=True), made to use a real bash on Windows."""
    real_run = subprocess.run
    bash = find_bash()

    def patched(argv, *a, **kw):
        if isinstance(argv, list) and argv and argv[0] == "bash":
            argv = [bash, *argv[1:]]
        return real_run(argv, *a, **kw)
    monkeypatch.setattr(grade.subprocess, "run", patched)

    def verdict(task: str, diff: str) -> dict:
        path = tmp_path / f"{task}.diff"
        path.write_bytes(diff.encode())
        return grade.grade(task_dir(task), path, True, "unused")
    return verdict


def full_file_diff(archive: bytes, rel: str, new_content: str, tmp: Path) -> str:
    """Produce a real git diff replacing one file (so fixtures never hand-craft hunks)."""
    import asyncio
    from app.diffing import build_diff
    repo = safe_extract(archive, tmp)
    (repo / rel).write_bytes(new_content.encode())
    return asyncio.run(build_diff(repo)).diff


PASSING_DERIVED = DerivedChecks(
    files={},
    command=("PYTHONPATH=src python3 -c \"from pricing.cart import total, Item; "
             "assert total([], 0.2) == 0.0; assert total([Item('a', 10.0)], 0) == 10.0\""))


def gate(archive, diff, sandbox, tmp_path, **kw):
    profile_cmd = kw.pop("test_command", "bash run_tests.sh")
    return run(run_gate(archive, diff, test_command=profile_cmd, sandbox=sandbox,
                        scratch_parent=tmp_path / "gate", timeout=kw.pop("timeout", 120), **kw))


def test_fix_01_is_tier_a_and_the_grader_agrees(task_archive, sandbox, tmp_path, grade_local):
    result = gate(task_archive("01-pricing-tax"), FIX_01, sandbox, tmp_path, derived=PASSING_DERIVED)
    assert result.tier is Tier.A, result.feedback()
    assert grade_local("01-pricing-tax", FIX_01)["verdict"] == "accept"


def test_fix_07_bash_is_tier_a_and_the_grader_agrees(task_archive, sandbox, tmp_path, grade_local):
    diff = full_file_diff(task_archive("07-bash-semver-bump"), "bin/bump_version.sh", BUMP_07, tmp_path / "s")
    check = DerivedChecks(files={}, command="bash bin/bump_version.sh v1.2.3 patch | grep -qx v1.2.4")
    result = gate(task_archive("07-bash-semver-bump"), diff, sandbox, tmp_path, derived=check)
    assert result.tier is Tier.A, result.feedback()
    assert grade_local("07-bash-semver-bump", diff)["verdict"] == "accept"


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_fix_06_js_is_tier_a_and_the_grader_agrees(task_archive, sandbox, tmp_path, grade_local):
    diff = full_file_diff(task_archive("06-js-parse-duration"), "src/parseDuration.js", PARSE_06, tmp_path / "s")
    check = DerivedChecks(files={}, command="node -e \"if (require('./src/parseDuration')('1h30m') !== 5400) process.exit(1)\"")
    result = gate(task_archive("06-js-parse-duration"), diff, sandbox, tmp_path, derived=check)
    assert result.tier is Tier.A, result.feedback()
    assert grade_local("06-js-parse-duration", diff)["verdict"] == "accept"


def test_context_drift_fails_at_apply_like_the_grader(task_archive, sandbox, tmp_path, grade_local):
    drifted = FIX_01.replace("* tax_rate, 2)", "* rate, 2)")
    result = gate(task_archive("01-pricing-tax"), drifted, sandbox, tmp_path)
    assert (result.tier, result.stage) == (Tier.C, "apply")
    assert grade_local("01-pricing-tax", drifted)["stage"] == "apply"


def test_static_failure_matches_the_grader(task_archive, sandbox, tmp_path, grade_local):
    crlf = FIX_01.replace("\n", "\r\n")
    result = gate(task_archive("01-pricing-tax"), crlf, sandbox, tmp_path)
    assert (result.tier, result.stage, result.reason) == (Tier.C, "static", "CRLF line endings")
    assert grade_local("01-pricing-tax", crlf)["reason"] == "CRLF line endings"


def test_wrong_fix_fails_at_tests_with_useful_feedback(task_archive, sandbox, tmp_path):
    wrong = FIX_01.replace("(1 + tax_rate)", "(2 + tax_rate)")
    result = gate(task_archive("01-pricing-tax"), wrong, sandbox, tmp_path)
    assert (result.tier, result.stage) == (Tier.C, "tests")
    assert "test_total_applies_tax" in result.feedback()


def test_editing_existing_tests_is_rejected_by_policy(task_archive, sandbox, tmp_path):
    archive = task_archive("01-pricing-tax")
    diff = full_file_diff(archive, "tests/test_cart.py", "", tmp_path / "s")
    protected = profile_repo(safe_extract(archive, tmp_path / "p")).test_files
    result = gate(archive, diff, sandbox, tmp_path, protected_paths=protected)
    assert (result.tier, result.stage) == (Tier.C, "policy")


def test_derived_checks_decide_between_tier_a_and_tier_b(task_archive, sandbox, tmp_path):
    """Tier A needs the derived checks to pass; without them a repo-test-verified diff is Tier B."""
    archive = task_archive("01-pricing-tax")
    failing = DerivedChecks(files={"zz_derived_check.py": "raise SystemExit(1)\n"},
                            command="python3 zz_derived_check.py")
    assert gate(archive, FIX_01, sandbox, tmp_path, derived=PASSING_DERIVED).tier is Tier.A

    failed = gate(archive, FIX_01, sandbox, tmp_path, derived=failing)
    assert (failed.tier, failed.stage, failed.deliverable) == (Tier.B, "derived", True)

    missing = gate(archive, FIX_01, sandbox, tmp_path, derived=None)
    assert (missing.tier, missing.stage, missing.deliverable) == (Tier.B, "derived", True)

    # With no repository tests either, the derived checks were the only evidence.
    only_derived = gate(archive, FIX_01, sandbox, tmp_path, derived=failing, test_command=None)
    assert (only_derived.tier, only_derived.deliverable) == (Tier.C, False)
    nothing = gate(archive, FIX_01, sandbox, tmp_path, derived=None, test_command=None)
    assert (nothing.tier, nothing.stage) == (Tier.C, "tests")


def test_gate_timeout_is_tier_c(task_archive, sandbox, tmp_path):
    result = gate(task_archive("01-pricing-tax"), FIX_01, sandbox, tmp_path,
                  test_command="sleep 30", timeout=3)
    assert (result.tier, result.stage) == (Tier.C, "tests") and "timed out" in result.reason


def test_gate_leaves_no_scratch_behind(task_archive, sandbox, tmp_path):
    gate(task_archive("01-pricing-tax"), FIX_01, sandbox, tmp_path)
    assert list((tmp_path / "gate").iterdir()) == []
