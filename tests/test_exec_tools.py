"""Execution tools: results, timeouts, isolation from the worktree, deadline awareness."""
from __future__ import annotations

import dataclasses

from app.gate import DerivedChecks

from conftest import run


def call(registry, ctx, name, **args):
    return run(registry.dispatch(ctx, name, args))


def test_run_tests_reports_failure_then_success(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "run_tests")
    assert r.is_error and "test_total_applies_tax" in r.output and "exit code 1" in r.output
    call(registry, ctx, "str_replace", path="src/pricing/cart.py",
         old_str="* tax_rate, 2)", new_str="* (1 + tax_rate), 2)")
    r = call(registry, ctx, "run_tests")
    assert not r.is_error and "passed" in r.output


def test_run_tests_includes_derived_checks(registry, make_ctx):
    derived = DerivedChecks(files={"zz_derived_check.sh": "echo derived-ran; exit 3\n"},
                            command="bash zz_derived_check.sh")
    ctx = make_ctx(derived=derived)
    r = call(registry, ctx, "run_tests")
    assert r.is_error and "derived-ran" in r.output and "task-derived checks -> exit code 3" in r.output
    assert not (ctx.root / "zz_derived_check.sh").exists()        # overlay never lands in the worktree


def test_run_command_changes_are_discarded(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "run_command", command="mkdir -p build && echo hi > build/x && echo made")
    assert not r.is_error and "made" in r.output
    assert not (ctx.root / "build").exists()


def test_output_keeps_real_stdout_stderr_order(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "run_command", command="echo one; echo two >&2; echo three")
    assert r.output.splitlines()[1:] == ["one", "two", "three"]


def test_run_command_failure_and_empty(registry, make_ctx):
    ctx = make_ctx()
    r = call(registry, ctx, "run_command", command="echo oops >&2; exit 7")
    assert r.is_error and "exit code 7" in r.output and "oops" in r.output
    assert call(registry, ctx, "run_command", command="   ").is_error


def test_run_command_timeout_kills_the_process_tree(registry, make_ctx, settings):
    ctx = make_ctx(settings_override=dataclasses.replace(settings, command_timeout_s=6,
                                                         min_exec_budget_s=1))
    marker = ctx.scratch_parent / "still-alive"
    # A child that would write the marker after 10s must be dead by then.
    cmd = f"(sleep 10; echo x > '{marker.as_posix()}') & sleep 30"
    r = call(registry, ctx, "run_command", command=cmd)
    assert r.is_error and "TIMED OUT" in r.output
    run(__import__("asyncio").sleep(6))
    assert not marker.exists()


def test_exec_refuses_when_deadline_is_nearly_gone(registry, make_ctx):
    ctx = make_ctx(deadline_s=2)
    r = call(registry, ctx, "run_tests")
    assert r.is_error and "not enough time" in r.output


def test_submit_requires_an_edit(registry, make_ctx):
    ctx = make_ctx()
    assert call(registry, ctx, "submit", summary="done").is_error
    call(registry, ctx, "write_file", path="notes.txt", content="x\n")
    r = call(registry, ctx, "submit", summary="done")
    assert not r.is_error and ctx.state.submitted and ctx.state.summary == "done"
