"""Provider-neutral tool definitions and the dispatcher.

A ToolSpec is the single definition of a tool: name, description, strict JSON
schema and async handler. Adapters turn specs into a provider's format
(to_openai_tools below); an MCP adapter could serve the same specs later.

Handlers raise ToolError for expected failures (bad path, no match, ...).
dispatch() turns those, bad arguments and unexpected exceptions into an
is_error result, so a tool can never crash the agent loop.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from ..config import Settings
from ..deadline import Deadline
from ..gate import DerivedChecks
from ..sandbox import Sandbox
from ..textutil import truncate_middle
from .jail import PathJail

log = logging.getLogger("codefix.tools")


class ToolError(Exception):
    """An expected tool failure; the message is shown to the model."""


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


@dataclass
class ToolState:
    submitted: bool = False
    summary: str = ""
    edited_files: list[str] = field(default_factory=list)

    def note_edit(self, rel: str) -> None:
        if rel not in self.edited_files:
            self.edited_files.append(rel)


@dataclass
class ToolContext:
    """Everything one candidate's tools may touch. One instance per candidate."""
    jail: PathJail
    sandbox: Sandbox
    settings: Settings
    deadline: Deadline
    scratch_parent: Path
    test_command: str | None
    derived: DerivedChecks | None = None
    state: ToolState = field(default_factory=ToolState)

    @property
    def root(self) -> Path:
        return self.jail.root


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]      # JSON Schema, OpenAI strict-mode compatible
    handler: Handler


def obj_schema(**properties: dict[str, Any]) -> dict[str, Any]:
    """Strict object schema: every property required, no extras (use ["x","null"] for optional)."""
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def to_openai_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Responses API function-tool definitions."""
    return [{"type": "function", "name": s.name, "description": s.description,
             "parameters": s.parameters, "strict": True} for s in specs]


def to_chat_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Chat Completions function-tool definitions (OpenAI-compatible; used via OpenRouter)."""
    return [{"type": "function",
             "function": {"name": s.name, "description": s.description,
                          "parameters": s.parameters, "strict": True}} for s in specs]


_JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "null": type(None)}


def validate_args(schema: dict[str, Any], args: Any) -> None:
    """Minimal validator for the flat strict schemas used by our tools."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    props = schema.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing argument(s): {', '.join(missing)}")
    for key, value in args.items():
        allowed = props[key].get("type")
        allowed = allowed if isinstance(allowed, list) else [allowed]
        ok = any(isinstance(value, _JSON_TYPES[t]) and not (t == "integer" and isinstance(value, bool))
                 for t in allowed if t in _JSON_TYPES)
        if not ok:
            raise ToolError(f"argument '{key}' must be of type {' or '.join(allowed)}")


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool names")
        self.specs = specs
        self._by_name = {s.name: s for s in specs}

    def names(self) -> list[str]:
        return list(self._by_name)

    def openai_tools(self) -> list[dict[str, Any]]:
        return to_openai_tools(self.specs)

    def chat_tools(self) -> list[dict[str, Any]]:
        return to_chat_tools(self.specs)

    async def dispatch(self, ctx: ToolContext, name: str, raw_args: str | Mapping[str, Any]) -> ToolResult:
        started = time.monotonic()
        spec = self._by_name.get(name)
        try:
            if spec is None:
                raise ToolError(f"unknown tool '{name}'. Available: {', '.join(self.names())}")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError as exc:
                    raise ToolError(f"arguments are not valid JSON: {exc}") from exc
            else:
                args = dict(raw_args)
            validate_args(spec.parameters, args)
            output = await spec.handler(ctx, args)
            result = ToolResult(output)
        except ToolError as exc:
            result = ToolResult(f"Error: {exc}", is_error=True)
        except Exception as exc:  # a tool bug must not kill the loop
            log.exception("tool %s crashed", name)
            result = ToolResult(f"Error: internal tool failure ({type(exc).__name__}: {exc})",
                                is_error=True)
        result.output = truncate_middle(result.output, ctx.settings.tool_output_max_chars)
        log.info("tool=%s is_error=%s ms=%d", name, result.is_error,
                 int((time.monotonic() - started) * 1000))
        return result
