"""File tools: list_dir, read_file, search, str_replace, write_file.

Design follows SWE-agent's agent-computer interface findings: a windowed,
line-numbered viewer instead of dumping files; compact search results; edits
that must match exactly once and are refused (not half-applied) when they
would break Python syntax; short, specific error messages that tell the model
what to do next.

Files are read and written as bytes so line endings are never changed
(Python's text mode would turn \n into \r\n on Windows).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..profiler import SKIP_DIRS
from ..textutil import is_probably_binary
from .base import ToolContext, ToolError, ToolSpec, obj_schema
from .jail import JailError

MAX_EDIT_FILE_BYTES = 2_000_000


def _resolve(ctx: ToolContext, rel: str, *, write: bool = False) -> tuple[Path, str]:
    try:
        return ctx.jail.resolve_for_write(rel) if write else ctx.jail.resolve(rel)
    except JailError as exc:
        raise ToolError(str(exc)) from exc


def _read_text(path: Path, norm: str) -> str:
    if not path.exists():
        raise ToolError(f"'{norm}' does not exist")
    if path.is_dir():
        raise ToolError(f"'{norm}' is a directory; use list_dir")
    data = path.read_bytes()
    if is_probably_binary(data):
        raise ToolError(f"'{norm}' is a binary file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"'{norm}' is not valid UTF-8 text") from None


def _syntax_guard(norm: str, text: str) -> None:
    """Refuse edits that leave a Python file syntactically invalid."""
    if norm.endswith(".py"):
        try:
            compile(text, norm, "exec", dont_inherit=True)
        except SyntaxError as exc:
            raise ToolError(f"edit refused: it would leave '{norm}' with a syntax error "
                            f"(line {exc.lineno}: {exc.msg}). The file was not changed.") from None


def _numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines)))
    return "\n".join(f"{str(i).rjust(width)}| {line}" for i, line in enumerate(lines, start))


def _snippet(text: str, first_line: int, n_lines: int, context: int = 4) -> str:
    lines = text.split("\n")
    lo = max(1, first_line - context)
    hi = min(len(lines), first_line + n_lines - 1 + context)
    return _numbered(lines[lo - 1:hi], lo)


# ------------------------------------------------------------------ list_dir
async def list_dir(ctx: ToolContext, args: dict[str, Any]) -> str:
    path, norm = _resolve(ctx, args["path"])
    if not path.exists():
        raise ToolError(f"'{norm}' does not exist")
    if not path.is_dir():
        raise ToolError(f"'{norm}' is a file; use read_file")
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if child.name in SKIP_DIRS or child.is_symlink():
            continue
        entries.append(f"{child.name}/" if child.is_dir() else f"{child.name} ({child.stat().st_size} B)")
    cap = ctx.settings.list_dir_max_entries
    shown = entries[:cap]
    more = f"\n... {len(entries) - cap} more entries" if len(entries) > cap else ""
    return f"{norm}:\n" + ("\n".join(shown) if shown else "(empty)") + more


LIST_DIR = ToolSpec(
    name="list_dir",
    description="List files and folders in a repository directory. Use '.' for the root.",
    parameters=obj_schema(path={"type": "string", "description": "Directory relative to the repository root."}),
    handler=list_dir,
)


# ------------------------------------------------------------------ read_file
async def read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path, norm = _resolve(ctx, args["path"])
    text = _read_text(path, norm)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    window = ctx.settings.read_window_lines
    start = 1 if args.get("start_line") is None else args["start_line"]
    end = start + window - 1 if args.get("end_line") is None else args["end_line"]
    if start < 1:
        raise ToolError("start_line must be >= 1")
    if total == 0:
        return f"{norm} is empty (0 lines)"
    if start > total:
        raise ToolError(f"start_line {start} is past the end of '{norm}' ({total} lines)")
    if end < start:
        raise ToolError("end_line must be >= start_line")
    end = min(end, total, start + window - 1)
    header = f"{norm} (lines {start}-{end} of {total})"
    body = _numbered(lines[start - 1:end], start)
    footer = f"\n... {total - end} more lines; call read_file with start_line={end + 1}" if end < total else ""
    return f"{header}\n{body}{footer}"


READ_FILE = ToolSpec(
    name="read_file",
    description=("Read a text file with line numbers, up to 250 lines per call. "
                 "Pass null for start_line/end_line to read from the top."),
    parameters=obj_schema(
        path={"type": "string", "description": "File relative to the repository root."},
        start_line={"type": ["integer", "null"], "description": "First line (1-based), or null."},
        end_line={"type": ["integer", "null"], "description": "Last line (inclusive), or null."},
    ),
    handler=read_file,
)


# ------------------------------------------------------------------ search
async def search(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        pattern = re.compile(args["pattern"])
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from None
    base, norm = _resolve(ctx, args.get("path") or ".")
    if not base.exists():
        raise ToolError(f"'{norm}' does not exist")
    files = [base] if base.is_file() else None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            files += [Path(dirpath) / f for f in sorted(filenames)]
    hits: list[str] = []
    cap = ctx.settings.search_max_hits
    total = 0
    for f in files:
        if f.is_symlink() or f.stat().st_size > ctx.settings.search_max_file_bytes:
            continue
        data = f.read_bytes()
        if is_probably_binary(data):
            continue
        rel = ctx.jail.relative(f)
        for i, line in enumerate(data.decode("utf-8", errors="replace").split("\n"), 1):
            if pattern.search(line):
                total += 1
                if len(hits) < cap:
                    hits.append(f"{rel}:{i}: {line.strip()[:200]}")
    if not hits:
        return f"no matches for /{args['pattern']}/ in {norm}"
    more = f"\n... {total - cap} more matches; narrow the pattern or path" if total > cap else ""
    return "\n".join(hits) + more


SEARCH = ToolSpec(
    name="search",
    description="Search repository text files with a Python regular expression. Returns file:line: text.",
    parameters=obj_schema(
        pattern={"type": "string", "description": "Python regular expression."},
        path={"type": ["string", "null"], "description": "File or directory to search, or null for the whole repository."},
    ),
    handler=search,
)


# ------------------------------------------------------------------ str_replace
async def str_replace(ctx: ToolContext, args: dict[str, Any]) -> str:
    path, norm = _resolve(ctx, args["path"], write=True)
    old, new = args["old_str"], args["new_str"]
    if not old:
        raise ToolError("old_str must not be empty; use write_file to create a file")
    if old == new:
        raise ToolError("old_str and new_str are identical; nothing to change")
    if path.exists() and path.stat().st_size > MAX_EDIT_FILE_BYTES:
        raise ToolError(f"'{norm}' is too large to edit")
    text = _read_text(path, norm)
    if "\r\n" in text and "\r\n" not in old:     # tolerate LF-only old_str on CRLF files
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old_str was not found in '{norm}'. It must match the file exactly, "
                        "including whitespace and indentation; read the file again and copy the text.")
    if count > 1:
        lines = [text.count("\n", 0, m.start()) + 1 for m in re.finditer(re.escape(old), text)]
        raise ToolError(f"old_str matches {count} places in '{norm}' (lines {', '.join(map(str, lines))}); "
                        "include more surrounding lines so it matches exactly once.")
    start = text.index(old)
    updated = text[:start] + new + text[start + len(old):]
    _syntax_guard(norm, updated)
    path.write_bytes(updated.encode("utf-8"))
    ctx.state.note_edit(norm)
    first_line = text.count("\n", 0, start) + 1
    return f"Edited {norm}. Current content around the change:\n" + \
        _snippet(updated, first_line, new.count("\n") + 1)


STR_REPLACE = ToolSpec(
    name="str_replace",
    description=("Replace one exact occurrence of old_str with new_str in a file. old_str must match "
                 "the file exactly once, including whitespace. Existing test files are read-only."),
    parameters=obj_schema(
        path={"type": "string", "description": "File relative to the repository root."},
        old_str={"type": "string", "description": "Exact text to replace (must occur exactly once)."},
        new_str={"type": "string", "description": "Replacement text."},
    ),
    handler=str_replace,
)


# ------------------------------------------------------------------ write_file
async def write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path, norm = _resolve(ctx, args["path"], write=True)
    content = args["content"]
    _syntax_guard(norm, content)
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    ctx.state.note_edit(norm)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"{'Overwrote' if existed else 'Created'} {norm} ({n} lines)."


WRITE_FILE = ToolSpec(
    name="write_file",
    description=("Create a new file, or replace the whole content of an existing source file. "
                 "Prefer str_replace for small changes. Existing test files are read-only."),
    parameters=obj_schema(
        path={"type": "string", "description": "File relative to the repository root."},
        content={"type": "string", "description": "Complete file content."},
    ),
    handler=write_file,
)
