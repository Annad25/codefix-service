"""Tool definitions meet OpenAI strict mode, and the dispatcher never crashes."""
from __future__ import annotations

import re

from app.tools import ALL_TOOLS, ToolRegistry, ToolSpec
from app.tools.base import obj_schema

from conftest import run


def _check_strict(schema: dict, where: str) -> None:
    assert schema["type"] == "object", where
    assert schema.get("additionalProperties") is False, where
    assert sorted(schema["required"]) == sorted(schema["properties"]), where
    for key, prop in schema["properties"].items():
        types = prop["type"] if isinstance(prop["type"], list) else [prop["type"]]
        assert set(types) <= {"string", "integer", "number", "boolean", "null", "object", "array"}, key
        assert prop.get("description"), f"{where}.{key} needs a description"
        if "object" in types:
            _check_strict(prop, f"{where}.{key}")


def test_every_tool_is_strict_mode_compatible():
    for spec in ALL_TOOLS:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", spec.name)
        assert len(spec.description) > 20
        _check_strict(spec.parameters, spec.name)


def test_openai_format():
    tools = ToolRegistry(list(ALL_TOOLS)).openai_tools()
    assert all(t["type"] == "function" and t["strict"] is True for t in tools)


def test_dispatch_turns_every_failure_into_is_error(registry, make_ctx):
    ctx = make_ctx()
    cases = [
        ("no_such_tool", {}), ("read_file", "{not json"), ("read_file", {"path": "x"}),
        ("read_file", {"path": "x", "start_line": None, "end_line": None, "extra": 1}),
        ("read_file", {"path": 5, "start_line": None, "end_line": None}),
        ("read_file", {"path": "x", "start_line": True, "end_line": None}),
        ("list_dir", []),
    ]
    for name, args in cases:
        r = run(registry.dispatch(ctx, name, args))
        assert r.is_error and r.output.startswith("Error:"), (name, args, r.output)


def test_handler_crash_is_contained(make_ctx):
    async def boom(ctx, args):
        raise RuntimeError("kaboom")
    reg = ToolRegistry([ToolSpec("boom", "A tool that always crashes.", obj_schema(), boom)])
    r = run(reg.dispatch(make_ctx(), "boom", "{}"))
    assert r.is_error and "internal tool failure" in r.output and "kaboom" in r.output


def test_output_is_truncated(registry, make_ctx):
    ctx = make_ctx()
    r = run(registry.dispatch(ctx, "run_command", {"command": "python3 -c \"print('x' * 100000)\""}))
    assert len(r.output) <= ctx.settings.tool_output_max_chars + 100 and "truncated" in r.output
