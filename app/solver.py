"""The solve pipeline.

    snapshot ─► profile + baseline tests ─┐
                spec checks (LLM) ────────┼─► candidate 1 (primary model)
                                          │     edit ─► own checks ─► gate ─► repair ... (max_repairs)
                                          └─► candidate 2 (secondary model), only if 1 did not reach tier A
                                                and time and budget remain
    ─► best gate-passed diff (Tier A, else Tier B) or null, always before the deadline

Every candidate's change is checked twice: first by the candidate path itself
(repository tests + derived checks on a copy of its worktree), then, only if
that passes, by the independent delivery gate (fresh extraction of the original
archive, grader's static rules, git apply, same checks). Nothing reaches the
response unless the gate passed it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .deadline import Deadline
from .diffing import DiffError, build_diff
from .editformat import EditFormatError, apply_edit_blocks, describe_outcomes, parse_edit_blocks
from .gate import GateResult, Tier, run_gate
from .llm import BudgetExceeded, CostLedger, LLMClient, LLMError
from .procs import ProcResult
from .profiler import RepoProfile, profile_repo
from .prompts import FIX_SYSTEM, fix_user_message, repair_user_message
from .sandbox import Sandbox, run_in_copy
from .specchecks import SpecResult, generate_spec_checks
from .textutil import tail_lines, truncate_middle
from .tools import PathJail, ToolContext
from .workspace import UnsafeArchiveError, Workspace

log = logging.getLogger("codefix.solver")

RECORD_OUTPUT_CHARS = 8_000
# Minimum seconds kept back for the gate, by language (cold builds are slow).
GATE_FLOOR_S = {"python": 6, "javascript": 6, "typescript": 15, "bash": 4, "c": 8, "cpp": 10,
                "java": 25, "go": 35, "rust": 60}
MIN_ROUND_S = 15.0          # do not start an LLM round with less agent time than this
MIN_CANDIDATE_S = 45.0      # do not start a second candidate with less than this
MAX_TRUNCATION_RETRIES = 2  # a cut-off answer is not a repair attempt, so it does not consume a round


# ------------------------------------------------------------------ record
class Recorder:
    """Builds the contract's `record`: ordered model turns and tool results."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def text(self, text: str) -> None:
        self.entries.append({"role": "assistant", "type": "text", "text": text})

    def tool_call(self, name: str, input: dict[str, Any]) -> None:
        self.entries.append({"role": "assistant", "type": "tool_call", "name": name, "input": input})

    def tool_result(self, name: str, output: str, is_error: bool) -> None:
        self.entries.append({"role": "tool", "name": name,
                             "output": truncate_middle(output, RECORD_OUTPUT_CHARS), "is_error": is_error})


# ------------------------------------------------------------------ data
@dataclass
class SolveRequest:
    request_id: str
    archive: bytes
    task: str
    deadline_seconds: float
    received_at: float = field(default_factory=time.monotonic)


@dataclass
class Candidate:
    name: str
    model: str
    record: Recorder = field(default_factory=Recorder)
    diff: str | None = None
    gate: GateResult | None = None
    rounds: int = 0

    def snapshot(self) -> "Candidate":
        """Frozen copy: later repair rounds must not alter a diff already chosen for delivery."""
        rec = Recorder()
        rec.entries = list(self.record.entries)
        return Candidate(name=self.name, model=self.model, record=rec, diff=self.diff, gate=self.gate,
                         rounds=self.rounds)

    def rank(self) -> tuple:
        """Higher is better: tier, derived checks passed, then smaller diffs."""
        tier = {Tier.A: 2, Tier.B: 1}.get(self.gate.tier, 0) if self.gate else -1
        derived = 1 if self.gate and self.gate.derived_ok else 0
        return (tier, derived, -len(self.diff or ""))


@dataclass
class SolveState:
    """Shared with the watchdog, so a timeout can still return the best gate-passed diff."""
    started: float = field(default_factory=time.monotonic)
    spec_record: Recorder = field(default_factory=Recorder)
    candidates: list[Candidate] = field(default_factory=list)
    best: Candidate | None = None
    notes: list[str] = field(default_factory=list)
    ledger: CostLedger | None = None

    def offer(self, cand: Candidate) -> None:
        if cand.gate and cand.gate.deliverable and (self.best is None or cand.rank() > self.best.rank()):
            self.best = cand.snapshot()

    def note(self, text: str) -> None:
        log.info(text)
        self.notes.append(f"{time.monotonic() - self.started:6.1f}s {text}")


@dataclass
class SolveResult:
    request_id: str
    diff: str | None
    record: list[dict[str, Any]]
    usage: dict[str, Any]
    meta: dict[str, Any]

    def response(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "diff": self.diff, "record": self.record, "usage": self.usage}


# ------------------------------------------------------------------ solver
class Solver:
    def __init__(self, settings: Settings, llm: LLMClient, sandbox: Sandbox) -> None:
        self.settings = settings
        self.llm = llm
        self.sandbox = sandbox

    async def solve(self, req: SolveRequest) -> SolveResult:
        """Never raises and always returns before the deadline (minus the transfer margin)."""
        budget = max(req.deadline_seconds, self.settings.budget_floor_s)
        hard = Deadline.for_request(budget, started_at=req.received_at)
        state = SolveState(ledger=self.llm.new_request_ledger())
        try:
            await asyncio.wait_for(self._solve(req, hard, state), timeout=max(0.1, hard.remaining()))
        except asyncio.TimeoutError:
            state.note("watchdog: deadline reached, returning the best gate-passed diff so far")
        except Exception as exc:  # the service must answer even if something unexpected breaks
            log.exception("solve failed")
            state.note(f"internal error: {type(exc).__name__}: {exc}")
        return self._result(req, state)

    # -------------------------------------------------------------- pipeline
    async def _solve(self, req: SolveRequest, hard: Deadline, state: SolveState) -> None:
        try:
            ws = Workspace.create(req.archive, self.settings.work_root, req.request_id)
        except UnsafeArchiveError as exc:
            state.note(f"rejected snapshot: {exc}")
            return
        try:
            profile = profile_repo(ws.pristine)
            state.note(f"profile: language={profile.language} test_command={profile.test_command!r} "
                       f"({profile.test_command_source}), {len(profile.files)} files")
            baseline = asyncio.create_task(self._baseline(ws, profile, hard))
            spec_task = None
            if self.settings.spec_checks_enabled:
                spec_task = asyncio.create_task(self._spec(req, ws, profile, hard, state))
            try:
                models = [self.settings.model_primary, self.settings.model_secondary][:self.settings.max_candidates]
                for k, model in enumerate(models, 1):
                    if k > 1:
                        if state.best and state.best.gate and state.best.gate.tier is Tier.A:
                            break
                        left = self._agent_deadline(hard, profile, baseline).remaining()
                        if left < MIN_CANDIDATE_S:
                            state.note(f"skipping candidate {k}: only {left:.0f}s of agent time left")
                            break
                    cand = Candidate(name=f"c{k}", model=model)
                    state.candidates.append(cand)
                    try:
                        await self._run_candidate(cand, req, ws, profile, hard, baseline, spec_task, state)
                    except BudgetExceeded as exc:
                        state.note(f"stopping: {exc}")
                        break
            finally:
                pending = [t for t in (baseline, spec_task) if t and not t.done()]
                for task in pending:
                    task.cancel()
                # Let cancelled work kill and reap its processes before the workspace is deleted.
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            ws.cleanup()

    async def _baseline(self, ws: Workspace, profile: RepoProfile, hard: Deadline) -> ProcResult | None:
        if profile.test_command is None:
            return None
        return await run_in_copy(self.sandbox, ws.pristine, profile.test_command,
                                 timeout=hard.timeout(min(120.0, self.settings.test_timeout_s)),
                                 scratch_parent=ws.scratch_dir())

    def _gate_reserve(self, profile: RepoProfile, baseline: asyncio.Task | None) -> float:
        floor = GATE_FLOOR_S.get(profile.language, 20)
        measured = 0.0
        if baseline is not None and baseline.done() and not baseline.cancelled() and baseline.exception() is None:
            result = baseline.result()
            measured = result.duration_s if result else 0.0
        runs = 2 if self.settings.spec_checks_enabled else 1        # repository tests + derived checks
        return max(floor, 1.5 * measured * runs) + 3.0

    def _agent_deadline(self, hard: Deadline, profile: RepoProfile, baseline) -> Deadline:
        return hard.shortened(self._gate_reserve(profile, baseline))

    async def _spec(self, req: SolveRequest, ws: Workspace, profile: RepoProfile, hard: Deadline,
                    state: SolveState) -> SpecResult:
        rec = state.spec_record
        rec.tool_call("write_task_checks", {"model": self.settings.model_spec})
        result = await generate_spec_checks(
            self.llm, model=self.settings.model_spec, fallback_models=list(self.settings.model_fallbacks),
            task=req.task, profile=profile, deadline=self._agent_deadline(hard, profile, None),
            ledger=state.ledger, sandbox=self.sandbox, scratch_parent=ws.scratch_dir(), pristine=ws.pristine,
            max_output_tokens=self.settings.spec_max_output_tokens)
        for answer in result.answers:          # the model's own turns belong in the record
            rec.text(answer)
        output = result.note
        if result.checks:
            (path, content), = result.checks.files.items()
            output += f"\ncommand: {result.checks.command}\n--- {path} ---\n{content}"
        rec.tool_result("write_task_checks", output, is_error=result.checks is None)
        state.note(result.note)
        return result

    async def _derived(self, spec_task, agent_deadline: Deadline):
        """Derived checks, waiting briefly for them if they are still being generated."""
        if spec_task is None:
            return None
        if not spec_task.done():
            try:
                wait = min(self.settings.spec_wait_s, max(0.0, agent_deadline.remaining() - MIN_ROUND_S))
                await asyncio.wait_for(asyncio.shield(spec_task), timeout=wait)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return None
        if spec_task.cancelled() or spec_task.exception() is not None:
            return None
        return spec_task.result().checks

    async def _run_candidate(self, cand: Candidate, req: SolveRequest, ws: Workspace, profile: RepoProfile,
                             hard: Deadline, baseline, spec_task, state: SolveState) -> None:
        wt = ws.new_worktree(cand.name)
        ctx = ToolContext(jail=PathJail(wt, frozenset(profile.test_files)), sandbox=self.sandbox,
                           settings=self.settings, deadline=hard, scratch_parent=ws.scratch_dir(),
                           test_command=profile.test_command)
        rec = cand.record
        # The derived checks are generated in parallel with this candidate's first
        # model call and attached when the change is verified (see _attempt), so
        # neither step waits on the other.
        messages: list[dict[str, Any]] = [{"role": "system", "content": FIX_SYSTEM},
                                          {"role": "user", "content": fix_user_message(req.task, profile)}]
        changed: list[str] = []
        truncations = 0
        rnd = 0
        while rnd < 1 + self.settings.max_repairs:
            agent_deadline = self._agent_deadline(hard, profile, baseline)
            if agent_deadline.remaining() < MIN_ROUND_S:
                state.note(f"{cand.name}: stopping, {agent_deadline.remaining():.0f}s of agent time left")
                return
            cand.rounds = rnd + 1
            try:
                reply = await self.llm.chat(model=cand.model, fallback_models=list(self.settings.model_fallbacks),
                                            messages=messages, deadline=agent_deadline, ledger=state.ledger,
                                            max_output_tokens=self.settings.fix_max_output_tokens)
            except BudgetExceeded:
                raise
            except LLMError as exc:
                state.note(f"{cand.name}: model call failed: {exc}")
                return
            text = reply.content or ""
            rec.text(text)
            messages.append({"role": "assistant", "content": text})
            state.note(f"{cand.name} round {rnd + 1}: {reply.model} replied "
                       f"({reply.usage.completion_tokens} tokens, ${reply.usage.cost_usd:.4f}"
                       f"{', CUT OFF at the output limit' if reply.finish_reason == 'length' else ''})")
            if reply.finish_reason == "length" and "REPLACE" not in text:
                # The answer never reached the edit blocks: a wasted call, not a failed
                # repair, so it does not consume a repair round.
                if truncations >= MAX_TRUNCATION_RETRIES:
                    state.note(f"{cand.name}: answers keep being cut off at the output limit; giving up")
                    return
                truncations += 1
                messages.append({"role": "user", "content":
                                 "Your answer was cut off at the output limit before any edit block. "
                                 "Reply again with at most 5 short requirement lines, then the "
                                 "SEARCH/REPLACE blocks only. No explanations, no repeated code."})
                continue

            feedback = await self._attempt(cand, text, ctx, ws, profile, hard, agent_deadline,
                                           spec_task, state, changed)
            if feedback is None:          # tier A, or nothing more to do
                return
            messages.append({"role": "user", "content": repair_user_message(feedback, wt, changed)})
            rnd += 1

    async def _attempt(self, cand: Candidate, text: str, ctx: ToolContext, ws: Workspace, profile: RepoProfile,
                       hard: Deadline, agent_deadline: Deadline, spec_task, state: SolveState,
                       changed: list[str]) -> str | None:
        """Apply one model answer and verify it. Returns repair feedback, or None to stop."""
        rec = cand.record
        try:
            blocks = parse_edit_blocks(text)
        except EditFormatError as exc:
            return f"Your answer could not be parsed: {exc}. Use the SEARCH/REPLACE format exactly."
        if not blocks:
            if "DERIVED_CHECK_WRONG" in text and cand.gate and cand.gate.tier is Tier.B:
                state.note(f"{cand.name}: model judged a derived check wrong; keeping the tier B change")
                return None
            return "No SEARCH/REPLACE blocks were found in your answer. Reply with edit blocks."

        rec.tool_call("apply_edits", {"blocks": [{"path": b.path, "search": b.search, "replace": b.replace}
                                                 for b in blocks]})
        outcomes = await apply_edit_blocks(ctx, blocks)
        failed = [o for o in outcomes if not o.ok]
        rec.tool_result("apply_edits", describe_outcomes(outcomes), is_error=bool(failed))
        edit_report = f"Edit results:\n{describe_outcomes(outcomes)}\n\n" if failed else ""

        try:
            built = await build_diff(ctx.root)
        except DiffError as exc:
            return f"{edit_report}The change cannot be delivered: {exc}"
        for f in built.files:
            if f not in changed:
                changed.append(f)
        if not built.diff:
            return f"{edit_report}Your edits produced no change to the repository."

        # 1. The candidate path's own evidence: repository tests + derived checks on a worktree copy.
        if ctx.derived is None:
            ctx.derived = await self._derived(spec_task, agent_deadline)
        tests_ok, derived_ok, report = await self._check(ctx, hard)
        rec.tool_call("run_checks", {"repository_tests": profile.test_command,
                                     "derived_checks": ctx.derived.command if ctx.derived else None})
        rec.tool_result("run_checks", report, is_error=not (tests_ok and derived_ok is not False))
        if not tests_ok:
            # The repository's own tests are the floor: without them there is nothing to deliver.
            return f"{edit_report}{report}"
        # Repository tests pass. Even if the derived checks failed, the gate still runs: it
        # records a Tier-B result that can be delivered if no Tier A arrives, and its output
        # is the feedback for the next repair round.

        # 2. The independent delivery gate.
        rec.tool_call("delivery_gate", {"diff_bytes": len(built.diff.encode())})
        gate = await run_gate(ws.archive, built.diff, test_command=profile.test_command, sandbox=self.sandbox,
                              scratch_parent=ws.scratch_dir(), timeout=hard.timeout(self.settings.test_timeout_s),
                              derived=ctx.derived, protected_paths=profile.test_files)
        rec.tool_result("delivery_gate", f"tier {gate.tier.value} at stage '{gate.stage}'"
                        + (f": {gate.reason}" if gate.reason else "") + f" ({gate.duration_s:.1f}s)",
                        is_error=gate.tier is Tier.C)
        cand.diff, cand.gate = built.diff, gate
        state.offer(cand)
        state.note(f"{cand.name}: gate tier {gate.tier.value} ({gate.stage})")
        if gate.tier is Tier.A:
            return None
        feedback = f"{edit_report}{gate.feedback()}"
        if gate.derived_ok is False and ctx.derived:
            # The failing assertion alone rarely shows the scenario; the test's setup does.
            (path, content), = ctx.derived.files.items()
            feedback += (f"\n\nThe task-derived test file (read-only: change the code, not the test):\n"
                         f"--- {path} ---\n{content.rstrip()}\n")
        return feedback

    async def _check(self, ctx: ToolContext, hard: Deadline) -> tuple[bool, bool | None, str]:
        sections: list[str] = []
        tests_ok = True
        if ctx.test_command:
            r = await run_in_copy(self.sandbox, ctx.root, ctx.test_command, scratch_parent=ctx.scratch_parent,
                                  timeout=hard.timeout(self.settings.test_timeout_s))
            tests_ok = r.ok
            sections.append(f"Repository tests (`{ctx.test_command}`): {'PASSED' if r.ok else 'FAILED'}\n"
                            f"{tail_lines(r.output, 60)}")
        derived_ok = None
        if ctx.derived and tests_ok:
            r = await run_in_copy(self.sandbox, ctx.root, ctx.derived.command, scratch_parent=ctx.scratch_parent,
                                  overlay=ctx.derived.files, timeout=hard.timeout(self.settings.test_timeout_s))
            derived_ok = r.ok
            sections.append(f"Task-derived checks (`{ctx.derived.command}`): {'PASSED' if r.ok else 'FAILED'}\n"
                            f"{tail_lines(r.output, 60)}")
        return tests_ok, derived_ok, "\n\n".join(sections) or "(no checks available)"

    # -------------------------------------------------------------- result
    def _result(self, req: SolveRequest, state: SolveState) -> SolveResult:
        best = state.best
        chosen = best if best and best.gate and best.gate.deliverable else None
        record_source = chosen or (state.candidates[-1] if state.candidates else None)
        record = list(state.spec_record.entries) + (record_source.record.entries if record_source else [])
        if not record:
            record = [{"role": "assistant", "type": "text", "text": "; ".join(state.notes) or "no work done"}]
        usage = state.ledger.usage.to_dict() if state.ledger else {}
        meta = {
            "tier": chosen.gate.tier.value if chosen else None,
            "candidate": chosen.name if chosen else None,
            "model": chosen.model if chosen else None,
            "rounds": {c.name: c.rounds for c in state.candidates},
            "elapsed_s": round(time.monotonic() - state.started, 2),
            "notes": state.notes,
        }
        result = SolveResult(request_id=req.request_id, diff=chosen.diff if chosen else None,
                             record=record, usage=usage, meta=meta)
        self._write_trace(req, state, result)
        return result

    def _write_trace(self, req: SolveRequest, state: SolveState, result: SolveResult) -> None:
        if self.settings.trace_dir is None:
            return
        try:
            self.settings.trace_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in req.request_id)[:48]
            path = self.settings.trace_dir / f"{safe}-{int(time.time())}.json"
            trace = {"request_id": req.request_id, "task": req.task, "deadline_seconds": req.deadline_seconds,
                     "diff": result.diff, "usage": result.usage, "meta": result.meta,
                     "spec_record": state.spec_record.entries,
                     "candidates": [{"name": c.name, "model": c.model, "rounds": c.rounds,
                                     "gate": c.gate.to_dict() if c.gate else None, "diff": c.diff,
                                     "record": c.record.entries} for c in state.candidates]}
            path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write trace: %s", exc)
