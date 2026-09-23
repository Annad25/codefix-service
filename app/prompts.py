"""Prompts and repository context.

System prompts are static strings (identical across requests) so providers can
cache them; everything request-specific goes in the user message after them.
The whole repository is inlined when it fits: task repositories are small, and
one well-informed call beats many exploratory tool calls on a free-tier quota.
"""
from __future__ import annotations

from pathlib import Path

from .profiler import RepoProfile
from .textutil import is_probably_binary

MAX_CONTEXT_CHARS = 120_000
MAX_FILE_CHARS = 40_000

FIX_SYSTEM = """You are a senior software engineer. You change a repository so that it satisfies a task description. \
Your change is graded by a hidden acceptance test suite written from the same task description, run offline in a clean \
copy of the repository. You never see those tests, so the task text is the specification: every sentence in it is a \
requirement that will be tested, including edge cases, error types, boundaries, ordering and return types.

Rules:
- Change source code only. Existing test files are read-only; do not create files named acceptance* or zz_*.
- Keep existing function signatures, names, exports and file locations unless the task says otherwise.
- Use only the language's standard library. No network, no new dependencies, no build-tool changes.
- Respect the stated toolchain versions; do not use language features newer than them.
- Keep the change minimal and focused, but complete: handle every requirement in the task.
- Where the task says a variation is allowed (letter case, extra whitespace, optional prefixes), accept it everywhere it can appear, including at the start and end of the input.
- Where a rule says an input is ignored, clamped or treated as zero, do not let it change stored state either: later calls must behave as if it never happened.

How to answer:
1. First, a numbered list of every requirement in the task, including edge cases: at most 10 lines, a few words each.
2. Then the edits, as one or more SEARCH/REPLACE blocks in exactly this format:

path/to/file.ext
<<<<<<< SEARCH
exact lines currently in the file
=======
the new lines
>>>>>>> REPLACE

- SEARCH must copy the current file text exactly (whitespace included) and match exactly one place.
- To create a file or replace a whole file, leave SEARCH empty and put the full content in the replacement.
- Put nothing else after the blocks."""

REPAIR_INSTRUCTIONS = """Your previous change did not pass verification. Details are below, followed by the CURRENT \
content of every file you changed. Write new SEARCH/REPLACE blocks against the current content (not the original) to \
fix the problem. Re-check every requirement of the task, not only the failing one.
If a task-derived check clearly contradicts the task text, say DERIVED_CHECK_WRONG on its own line and explain \
why in one sentence instead of bending the code to it."""

SPEC_SYSTEM = """You write acceptance tests from a task description, before any implementation exists. The tests will \
be run against candidate implementations to decide whether they meet the specification.

Rules:
- Test every requirement stated in the task: each rule, edge case, error condition, boundary and return type. \
Do not test behaviour the task does not state.
- Use the repository's own language and test style (the existing tests show the conventions and import paths).
- Standard library only; tests must run offline, fast (a few seconds) and deterministically.
- Test only public behaviour named in the task (functions, classes, CLI arguments), never internal details.
- Where the task says a variation is allowed (letter case, extra whitespace, optional prefixes, ordering), test that variation in every position it can occur, including at the start and end of the input.
- Where a rule says an input is ignored, clamped or treated as zero, also assert the NEXT call after it: the rule must not have corrupted stored state.
- Put all tests in ONE new file whose file name starts with zz_derived (e.g. tests/zz_derived_test.py, \
tests/zz_derived.test.js, pkg/zz_derived_test.go, tests/zz_derived.rs, tests/zz_derived.sh, tests/zz_derived_check.c). \
Go test files must be in the package directory and use its package name.
- The command runs from the repository root and must exit non-zero if any test fails.

Answer in exactly this format and nothing else:

FILE: <path of the new test file>
```
<complete file content>
```
COMMAND: <one-line shell command that runs only this test file>"""


def _file_block(root: Path, rel: str, budget: int) -> str | None:
    data = (root / rel).read_bytes()
    if is_probably_binary(data):
        return None
    text = data.decode("utf-8", errors="replace")
    if len(text) > min(MAX_FILE_CHARS, budget):
        text = text[:min(MAX_FILE_CHARS, budget)] + "\n... [file truncated]"
    return f"--- {rel} ---\n{text.rstrip()}\n"


def repo_context(profile: RepoProfile, root: Path | None = None, max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Repository map plus the full text of source and test files, within a character budget.

    Source files come first (they are what gets edited), then tests (they show
    conventions and import paths), then other text files.
    """
    root = root or profile.root
    tests = set(profile.test_files)
    order = ([f for f in profile.source_files if not f.endswith((".md", ".txt"))]
             + [f for f in profile.files if f in tests]
             + [f for f in profile.source_files if f.endswith((".md", ".txt"))])
    parts = ["Repository files:\n" + profile.repo_map(), ""]
    used = sum(len(p) for p in parts)
    omitted = []
    for rel in order:
        block = _file_block(root, rel, max_chars - used)
        if block is None:
            continue
        if used + len(block) > max_chars:
            omitted.append(rel)
            continue
        parts.append(block)
        used += len(block)
    if omitted:
        parts.append(f"(not shown for length: {', '.join(omitted)})")
    return "\n".join(parts)


def fix_user_message(task: str, profile: RepoProfile) -> str:
    return (f"TASK:\n{task.strip()}\n\n"
            f"Language/toolchain: {profile.language}. {profile.toolchain_note}\n"
            f"Repository test command: {profile.test_command or '(none found)'}\n"
            f"Read-only test files: {', '.join(profile.test_files) or '(none)'}\n\n"
            f"{repo_context(profile)}")


def spec_user_message(task: str, profile: RepoProfile) -> str:
    return (f"TASK:\n{task.strip()}\n\n"
            f"Language/toolchain: {profile.language}. {profile.toolchain_note}\n"
            f"The repository's own test command is: {profile.test_command or '(none found)'}\n\n"
            f"{repo_context(profile)}")


def repair_user_message(feedback: str, root: Path, changed_files: list[str]) -> str:
    current = []
    for rel in changed_files:
        path = root / rel
        if path.is_file():
            current.append(f"--- {rel} (current) ---\n{path.read_text(encoding='utf-8', errors='replace').rstrip()}\n")
        else:
            current.append(f"--- {rel} (deleted) ---\n")
    return f"{REPAIR_INSTRUCTIONS}\n\nVERIFICATION RESULT:\n{feedback.strip()}\n\n" + "\n".join(current)
