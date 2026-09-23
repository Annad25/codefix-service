"""Execution tools: run_tests, run_command, and the submit control tool.

Both execution tools run in the sandbox against a disposable copy of the
worktree, so nothing they create (build outputs, caches, stray files) can
leak into the diff. Every run is capped by the request deadline.
"""
from __future__ import annotations

from typing import Any

from ..sandbox import run_in_copy
from .base import ToolContext, ToolError, ToolSpec, obj_schema


def _budget(ctx: ToolContext, cap: float) -> float:
    timeout = ctx.deadline.timeout(cap)
    if timeout < ctx.settings.min_exec_budget_s:
        raise ToolError("not enough time left to run this; call submit now")
    return timeout


def _status(r) -> str:
    if r.timed_out:
        return f"TIMED OUT after {r.duration_s:.0f}s"
    return f"exit code {r.returncode} ({'passed' if r.ok else 'failed'}) in {r.duration_s:.1f}s"


async def run_tests(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.test_command is None and ctx.derived is None:
        raise ToolError("this repository has no detected test command; use run_command")
    sections: list[str] = []
    failed = False
    if ctx.test_command is not None:
        r = await run_in_copy(ctx.sandbox, ctx.root, ctx.test_command, scratch_parent=ctx.scratch_parent,
                              timeout=_budget(ctx, ctx.settings.test_timeout_s))
        failed |= not r.ok
        sections.append(f"== repository tests: `{ctx.test_command}` -> {_status(r)} ==\n{r.output}")
    if ctx.derived is not None:
        r = await run_in_copy(ctx.sandbox, ctx.root, ctx.derived.command, scratch_parent=ctx.scratch_parent,
                              overlay=ctx.derived.files, timeout=_budget(ctx, ctx.settings.test_timeout_s))
        failed |= not r.ok
        sections.append(f"== task-derived checks -> {_status(r)} ==\n{r.output}")
    report = "\n\n".join(sections)
    if failed:
        raise ToolError("some checks failed.\n\n" + report)
    return report


RUN_TESTS = ToolSpec(
    name="run_tests",
    description=("Run the repository's test suite (and the task-derived checks, when available) "
                 "in the offline sandbox against your current changes."),
    parameters=obj_schema(),
    handler=run_tests,
)


async def run_command(ctx: ToolContext, args: dict[str, Any]) -> str:
    command = args["command"].strip()
    if not command:
        raise ToolError("command is empty")
    r = await run_in_copy(ctx.sandbox, ctx.root, command, scratch_parent=ctx.scratch_parent,
                          timeout=_budget(ctx, ctx.settings.command_timeout_s))
    report = f"`{command}` -> {_status(r)}\n{r.output}".rstrip()
    if not r.ok:
        raise ToolError(report)
    return report


RUN_COMMAND = ToolSpec(
    name="run_command",
    description=("Run a bash command in the offline sandbox, in a disposable copy of the repository "
                 "(file changes it makes are discarded; edit files with str_replace/write_file). "
                 "No network. Time limit 60s."),
    parameters=obj_schema(command={"type": "string", "description": "bash command, run from the repository root."}),
    handler=run_command,
)


async def submit(ctx: ToolContext, args: dict[str, Any]) -> str:
    if not ctx.state.edited_files:
        raise ToolError("you have not changed any file yet; make the change before submitting")
    ctx.state.submitted = True
    ctx.state.summary = args["summary"]
    return "Submitted. The change will now be verified by the delivery gate."


SUBMIT = ToolSpec(
    name="submit",
    description="Finish: call once the change is complete and the tests pass.",
    parameters=obj_schema(summary={"type": "string", "description": "One or two sentences on what was changed and why."}),
    handler=submit,
)
