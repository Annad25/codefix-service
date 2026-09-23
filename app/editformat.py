"""SEARCH/REPLACE edit blocks: the patch format the model writes.

Chosen over JSON (code inside JSON strings is where weaker models break
escaping) and over raw unified diffs (models get hunk headers and context
wrong; the grader's `git apply` is unforgiving). Agentless uses the same idea.

Format, one or more blocks:

    path/to/file.py
    <<<<<<< SEARCH
    exact existing lines
    =======
    replacement lines
    >>>>>>> REPLACE

An empty SEARCH section writes the whole file (create or replace).
Blocks are applied through the jailed tool handlers, so every rule the agent
tools enforce (path jail, read-only tests, syntax guard) applies here too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .tools.base import ToolContext, ToolError
from .tools.fs_tools import str_replace, write_file

SEARCH_RE = re.compile(r"^\s*<{5,9} ?SEARCH\s*$")
DIVIDER_RE = re.compile(r"^\s*={5,9}\s*$")
REPLACE_RE = re.compile(r"^\s*>{5,9} ?REPLACE\s*$")
_FENCE_RE = re.compile(r"^\s*```")


@dataclass
class EditBlock:
    path: str
    search: str
    replace: str


@dataclass
class EditOutcome:
    block: EditBlock
    ok: bool
    message: str


class EditFormatError(ValueError):
    pass


def _clean_path(line: str) -> str:
    path = line.strip().strip("`*").strip()
    for prefix in ("File:", "file:", "Path:", "path:", "#"):
        if path.startswith(prefix):
            path = path[len(prefix):].strip()
    return path.strip("`*\"' ")


def parse_edit_blocks(text: str) -> list[EditBlock]:
    """Extract every SEARCH/REPLACE block. The path is the nearest non-fence line above SEARCH."""
    lines = text.replace("\r\n", "\n").split("\n")
    blocks: list[EditBlock] = []
    i = 0
    while i < len(lines):
        if not SEARCH_RE.match(lines[i]):
            i += 1
            continue
        j = i - 1
        while j >= 0 and (not lines[j].strip() or _FENCE_RE.match(lines[j])):
            j -= 1
        if j < 0:
            raise EditFormatError("a SEARCH block has no file path above it")
        path = _clean_path(lines[j])
        k = i + 1
        search: list[str] = []
        while k < len(lines) and not DIVIDER_RE.match(lines[k]):
            search.append(lines[k])
            k += 1
        if k >= len(lines):
            raise EditFormatError(f"block for {path} has no ======= divider")
        k += 1
        replace: list[str] = []
        while k < len(lines) and not REPLACE_RE.match(lines[k]):
            replace.append(lines[k])
            k += 1
        if k >= len(lines):
            raise EditFormatError(f"block for {path} has no >>>>>>> REPLACE line")
        blocks.append(EditBlock(path=path, search="\n".join(search), replace="\n".join(replace)))
        i = k + 1
    return blocks


async def apply_edit_blocks(ctx: ToolContext, blocks: list[EditBlock]) -> list[EditOutcome]:
    """Apply blocks in order through the jailed tools. A failed block does not stop the rest."""
    outcomes: list[EditOutcome] = []
    for block in blocks:
        try:
            if block.search.strip() == "":
                content = block.replace if block.replace.endswith("\n") else block.replace + "\n"
                msg = await write_file(ctx, {"path": block.path, "content": content})
            else:
                msg = await _replace(ctx, block)
            outcomes.append(EditOutcome(block, True, msg.split("\n", 1)[0]))
        except ToolError as exc:
            outcomes.append(EditOutcome(block, False, str(exc)))
    return outcomes


async def _replace(ctx: ToolContext, block: EditBlock) -> str:
    try:
        return await str_replace(ctx, {"path": block.path, "old_str": block.search, "new_str": block.replace})
    except ToolError as exc:
        # Models often get trailing whitespace on lines wrong; retry once with it normalised.
        if "not found" not in str(exc):
            raise
        path, _ = ctx.jail.resolve(block.path)
        text = path.read_bytes().decode("utf-8", errors="replace")
        norm = lambda s: "\n".join(ln.rstrip() for ln in s.split("\n"))
        if text != norm(text) or norm(block.search) == block.search:
            raise
        # File has no trailing whitespace; the model's SEARCH did. Match on normalised text.
        return await str_replace(ctx, {"path": block.path, "old_str": norm(block.search),
                                       "new_str": norm(block.replace)})


def describe_outcomes(outcomes: list[EditOutcome]) -> str:
    return "\n".join(f"- {o.block.path}: {'applied' if o.ok else 'FAILED'}: {o.message}" for o in outcomes)
