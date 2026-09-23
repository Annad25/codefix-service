"""In-process agent tools (the agent-computer interface)."""
from __future__ import annotations

from .base import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec, ToolState
from .exec_tools import RUN_COMMAND, RUN_TESTS, SUBMIT
from .fs_tools import LIST_DIR, READ_FILE, SEARCH, STR_REPLACE, WRITE_FILE
from .jail import JailError, PathJail

ALL_TOOLS = [LIST_DIR, READ_FILE, SEARCH, STR_REPLACE, WRITE_FILE, RUN_TESTS, RUN_COMMAND, SUBMIT]


def default_registry() -> ToolRegistry:
    return ToolRegistry(list(ALL_TOOLS))


__all__ = ["ALL_TOOLS", "JailError", "PathJail", "ToolContext", "ToolError", "ToolRegistry",
           "ToolResult", "ToolSpec", "ToolState", "default_registry"]
