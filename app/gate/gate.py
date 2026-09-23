"""Gate run: static rules -> policy -> fresh apply -> repository tests -> derived checks.

The gate re-extracts the ORIGINAL request archive for every run, applies the
diff with the same commands the grader uses, and runs checks in the sandbox.
It returns a tier:

    A  static, apply, repository tests and derived checks all pass
    B  static, apply and repository tests pass, but the task-derived checks
       failed or could not be produced
    C  any earlier stage fails (never delivered)

Tier A is always preferred. A Tier-B diff is delivered only when no Tier-A diff
exists by the deadline: it has still passed the grader's static rules, applied
cleanly to a fresh snapshot and passed the repository's own tests, whereas a
null diff is a certain failure. Derived checks are generated for every request;
when they exist and fail, the failure is fed back for repair first.
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

from ..procs import git
from ..sandbox import Sandbox, run_in_copy
from ..textutil import tail_lines, truncate_middle
from ..workspace import UnsafeArchiveError, rmtree_force, safe_extract
from .policy import policy_violations
from .static_rules import static_check

OUTPUT_KEEP_CHARS = 6_000


class Tier(str, Enum):
    A = "A"
    B = "B"
    C = "C"


@dataclass(frozen=True)
class DerivedChecks:
    """Checks written from the task text: overlay files plus the command that runs them."""
    files: Mapping[str, str]
    command: str


@dataclass
class GateResult:
    tier: Tier
    stage: str                  # static | policy | apply | tests | derived | passed
    reason: str | None = None
    tests_ok: bool = False
    tests_output: str = ""
    derived_ok: bool | None = None      # None when there were no derived checks
    derived_output: str = ""
    duration_s: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def deliverable(self) -> bool:
        return self.tier in (Tier.A, Tier.B)

    def feedback(self, max_lines: int = 40) -> str:
        """Compact failure summary to hand back to the agent for repair."""
        if self.tier is Tier.A:
            return "The delivery gate accepted the change."
        parts = [f"The delivery gate rejected the change at stage '{self.stage}'."]
        if self.reason:
            parts.append(f"Reason: {self.reason}")
        if self.stage == "tests" and self.tests_output:
            parts.append("Repository test output (tail):\n" + tail_lines(self.tests_output, max_lines))
        if self.derived_ok is False and self.derived_output:
            parts.append("Task-derived check output (tail):\n" + tail_lines(self.derived_output, max_lines))
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tier"] = self.tier.value
        return d


async def run_gate(archive: bytes, diff: str, *, test_command: str | None, sandbox: Sandbox,
                   scratch_parent: Path, timeout: float, derived: DerivedChecks | None = None,
                   protected_paths: Iterable[str] = ()) -> GateResult:
    started = time.monotonic()
    end = started + timeout
    timings: dict[str, float] = {}

    def finish(result: GateResult) -> GateResult:
        result.duration_s = round(time.monotonic() - started, 3)
        result.timings = timings
        return result

    # 1. the grader's public rules, byte for byte
    try:
        diff_bytes = diff.encode("utf-8")
    except UnicodeEncodeError:
        return finish(GateResult(Tier.C, "static", "diff is not encodable as UTF-8"))
    reason = static_check(diff_bytes)
    if reason:
        return finish(GateResult(Tier.C, "static", reason))

    # 2. our stricter policy
    problems = policy_violations(diff, protected_paths)
    if problems:
        return finish(GateResult(Tier.C, "policy", "; ".join(problems)))

    if test_command is None and derived is None:
        return finish(GateResult(Tier.C, "tests", "no repository tests and no derived checks to run"))

    scratch_parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gate-", dir=scratch_parent))
    try:
        # 3. fresh snapshot from the original bytes, then the grader's apply commands
        t = time.monotonic()
        try:
            repo = safe_extract(archive, tmp / "snapshot")
        except UnsafeArchiveError as exc:
            return finish(GateResult(Tier.C, "apply", f"cannot extract snapshot: {exc}"))
        patch = tmp / "candidate.diff"
        patch.write_bytes(diff_bytes)
        check = await git(["apply", "--check", str(patch)], repo)
        if not check.ok:
            return finish(GateResult(Tier.C, "apply",
                                     "does not apply cleanly: " + check.output[:500]))
        applied = await git(["apply", str(patch)], repo)
        if not applied.ok:
            return finish(GateResult(Tier.C, "apply", "git apply failed: " + applied.output[:500]))
        timings["apply"] = round(time.monotonic() - t, 3)

        # 4. the repository's own tests, in a disposable copy
        tests_ok, tests_output = True, "(no repository test command)"
        if test_command is not None:
            t = time.monotonic()
            r = await run_in_copy(sandbox, repo, test_command, scratch_parent=tmp,
                                  timeout=max(0.01, end - time.monotonic()))
            timings["tests"] = round(time.monotonic() - t, 3)
            tests_output = truncate_middle(r.output, OUTPUT_KEEP_CHARS)
            tests_ok = r.ok
            if not tests_ok:
                why = "timed out" if r.timed_out else f"exit code {r.returncode}"
                return finish(GateResult(Tier.C, "tests", f"repository tests failed ({why})",
                                         tests_ok=False, tests_output=tests_output))

        # 5. task-derived checks, in another disposable copy
        if derived is None:
            # Generation failed or ran out of time. The repository's own tests are
            # weaker evidence than A, but far better evidence than delivering nothing.
            return finish(GateResult(Tier.B, "derived", "no task-derived check was produced",
                                     tests_ok=True, tests_output=tests_output))
        t = time.monotonic()
        r = await run_in_copy(sandbox, repo, derived.command, scratch_parent=tmp,
                              overlay=derived.files, timeout=max(0.01, end - time.monotonic()))
        timings["derived"] = round(time.monotonic() - t, 3)
        derived_output = truncate_middle(r.output, OUTPUT_KEEP_CHARS)
        if r.ok:
            return finish(GateResult(Tier.A, "passed", tests_ok=True, tests_output=tests_output,
                                     derived_ok=True, derived_output=derived_output))
        why = "timed out" if r.timed_out else f"exit code {r.returncode}"
        # With no repository tests, the derived checks were the only evidence: that is Tier C.
        tier = Tier.B if test_command is not None else Tier.C
        return finish(GateResult(tier, "derived", f"task-derived checks failed ({why})",
                                 tests_ok=True, tests_output=tests_output,
                                 derived_ok=False, derived_output=derived_output))
    finally:
        rmtree_force(tmp)
