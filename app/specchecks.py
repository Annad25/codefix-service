"""Task-derived checks: tests written from the task text alone, before any fix exists.

This is the "at least one check derived from the task description" the brief
requires, and plays the role of Agentless's reproduction tests and CodeT's
generated tests: candidates are judged against evidence produced
independently of them.

The generated file is an overlay: it is copied into sandbox runs, never into
the worktree or the delivered diff (its zz_derived name is reserved for that).

Weaker models often bend the answer format, so parsing is lenient about how the
path and command are written, and a malformed or truncated answer gets one
retry with the exact problem stated.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .deadline import Deadline
from .gate import DerivedChecks
from .llm import CostLedger, LLMClient, LLMError, LLMReply
from .profiler import RepoProfile
from .prompts import SPEC_SYSTEM, spec_user_message
from .sandbox import Sandbox, run_in_copy

log = logging.getLogger("codefix.spec")

MAX_FORMAT_RETRIES = 1

_PATH_CHARS = r"[\w./-]+"
_S = r"[ \t]*"                  # horizontal space only: a pattern must never run onto the next line
# "FILE: x", "**FILE:** `x`", "### File: x", "file - x"
_FILE_RE = re.compile(rf"^{_S}(?:#+{_S})?\**{_S}FILE{_S}\**{_S}[:\-]?{_S}\**{_S}`?({_PATH_CHARS})`?\**{_S}$",
                      re.MULTILINE | re.IGNORECASE)
# "### tests/zz_derived_test.py" or "`tests/zz_derived_test.py`" alone on a line
_BARE_PATH_RE = re.compile(rf"^{_S}(?:#+{_S})?\**`?({_PATH_CHARS}zz_derived{_PATH_CHARS})`?\**:?{_S}$",
                           re.MULTILINE)
# path as a comment on the first line of the code block: "# tests/zz_derived_test.py", "// ..."
_COMMENT_PATH_RE = re.compile(rf"^{_S}(?:#|//|/\*|--){_S}(?:file:?{_S})?({_PATH_CHARS}zz_derived{_PATH_CHARS})",
                              re.IGNORECASE)
_CMD_RE = re.compile(rf"^{_S}(?:#+{_S})?\**{_S}(?:COMMAND|RUN(?: WITH)?){_S}\**{_S}:?{_S}\**{_S}`*([^`\n]*?)`*{_S}$",
                     re.MULTILINE | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n[ \t]*```", re.DOTALL)

FORMAT_REMINDER = """Answer in exactly this format and nothing else:

FILE: <path of the new test file, named zz_derived...>
```
<complete file content>
```
COMMAND: <one-line shell command that runs only this test file>"""


class SpecFormatError(ValueError):
    pass


@dataclass
class SpecResult:
    checks: DerivedChecks | None
    reply: LLMReply | None
    note: str
    answers: list[str] = field(default_factory=list)     # raw model answers, for the record


def _outside(matches, fences) -> list:
    """Drop regex matches that fall inside a fenced code block."""
    spans = [(f.start(), f.end()) for f in fences]
    return [m for m in matches if not any(a <= m.start() < b for a, b in spans)]


def parse_spec_answer(text: str) -> DerivedChecks:
    fences = list(_FENCE_RE.finditer(text))
    if not fences:
        raise SpecFormatError("no fenced code block with the test file")

    candidates = _outside(_FILE_RE.finditer(text), fences) or _outside(_BARE_PATH_RE.finditer(text), fences)
    if candidates:
        m_file = candidates[0]
        path = m_file.group(1).strip()
        code = next((f for f in fences if f.start() > m_file.start()), None)
        if code is None:
            raise SpecFormatError("no fenced code block after FILE:")
    else:
        code = fences[0]
        m_comment = _COMMENT_PATH_RE.match(code.group(1).split("\n", 1)[0])
        if not m_comment:
            raise SpecFormatError("no FILE: line naming the test file")
        path = m_comment.group(1)

    command = ""
    cmds = _outside(_CMD_RE.finditer(text, code.end()), fences)
    if cmds:
        command = cmds[0].group(1).strip()
        if not command:            # "COMMAND:" followed by a fenced command block
            nxt = next((f for f in fences if f.start() > cmds[0].end()), None)
            if nxt:
                command = next((ln.strip() for ln in nxt.group(1).splitlines() if ln.strip()), "")
    if command.startswith("$ "):
        command = command[2:]
    if not command:
        raise SpecFormatError("no COMMAND: line after the code block")

    parts = PurePosixPath(path.replace("\\", "/")).parts
    if (not parts or path.startswith("/") or ".." in parts or parts[0] in (".git", "acceptance", "acceptance_tests")
            or not parts[-1].startswith("zz_derived")):
        raise SpecFormatError(f"test file path not allowed: {path!r} (must be relative and named zz_derived*)")
    normalized_path = "/".join(parts)
    # A syntactically valid `COMMAND: true` is not evidence that the generated
    # check ran.  Reject command-shaped no-ops while permitting language-native
    # runners such as `go test ./ring -run TestDerived`, which cannot name a
    # single source file literally.
    if command.strip().rstrip(";").strip() in {"true", ":", "exit 0"}:
        raise SpecFormatError("COMMAND must run the generated test, not a no-op")
    module_path = str(PurePosixPath(normalized_path).with_suffix("")).replace("/", ".")
    discovery_runner = re.search(r"\b(?:go|cargo)\s+test\b|\bnode\s+--test\b|\bpython3?\s+-m\s+unittest\s+discover\b",
                                 command)
    if normalized_path not in command.replace("\\", "/") and module_path not in command and not discovery_runner:
        raise SpecFormatError("COMMAND must reference the generated test file or module")
    content = code.group(1)
    if not content.endswith("\n"):
        content += "\n"
    return DerivedChecks(files={normalized_path: content}, command=command)


def _broken_harness(output: str, returncode: int | None, timed_out: bool) -> str | None:
    """Detect a check that cannot run at all (as opposed to failing because the code is unfinished)."""
    if timed_out:
        return "the check timed out on the original code"
    if returncode in (126, 127):
        return "the check command could not be executed"
    for marker in ("SyntaxError:", "IndentationError:", "syntax error near unexpected token"):
        if marker in output and "zz_derived" in output:
            return f"the generated test file itself is invalid ({marker.rstrip(':')})"
    return None


async def generate_spec_checks(llm: LLMClient, *, model: str, fallback_models: list[str], task: str,
                               profile: RepoProfile, deadline: Deadline, ledger: CostLedger,
                               sandbox: Sandbox, scratch_parent: Path, pristine: Path,
                               reasoning_effort: str | None = None,
                               max_output_tokens: int = 6_000) -> SpecResult:
    messages = [{"role": "system", "content": SPEC_SYSTEM},
                {"role": "user", "content": spec_user_message(task, profile)}]
    answers: list[str] = []
    reply: LLMReply | None = None
    checks: DerivedChecks | None = None
    problem = ""
    for attempt in range(1 + MAX_FORMAT_RETRIES):
        try:
            reply = await llm.chat(model=model, fallback_models=fallback_models, messages=messages,
                                   deadline=deadline, ledger=ledger, reasoning_effort=reasoning_effort,
                                   max_output_tokens=max_output_tokens)
        except LLMError as exc:
            return SpecResult(None, reply, f"spec check generation failed: {exc}", answers)
        text = reply.content or ""
        answers.append(text)
        try:
            checks = parse_spec_answer(text)
            break
        except SpecFormatError as exc:
            truncated = reply.finish_reason == "length"
            problem = f"{exc}{' (answer was cut off at the output limit)' if truncated else ''}"
            log.info("spec answer unusable (attempt %d): %s", attempt + 1, problem)
            hint = ("Your answer was cut off at the output limit: keep the file short (at most about 15 focused "
                    "test cases, no explanations). " if truncated else "")
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That answer could not be used: {problem}. {hint}{FORMAT_REMINDER}"}]
    if checks is None:
        return SpecResult(None, reply, f"spec check answer unusable: {problem}", answers)

    # Sanity run on the untouched snapshot: drop checks that cannot even run.
    timeout = deadline.timeout(60)
    if timeout < 3:
        return SpecResult(checks, reply, "derived checks generated (no time for a sanity run)", answers)
    r = await run_in_copy(sandbox, pristine, checks.command, timeout=timeout,
                          scratch_parent=scratch_parent, overlay=checks.files)
    broken = _broken_harness(r.output, r.returncode, r.timed_out)
    if broken:
        return SpecResult(None, reply, f"derived checks discarded: {broken}", answers)
    state = "fail" if not r.ok else "pass"
    return SpecResult(checks, reply, f"derived checks ready ({next(iter(checks.files))}); "
                                     f"they {state} on the original code", answers)
