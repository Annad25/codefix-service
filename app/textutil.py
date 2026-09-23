"""Small text helpers shared by tools and the gate."""
from __future__ import annotations


def truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head and tail of long output, dropping the middle.

    Test failures show up at both ends (the first error and the summary), so
    this keeps half the budget for each.
    """
    if len(text) <= max_chars:
        return text
    half = max(1, (max_chars - 60) // 2)
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n... [{dropped} characters truncated] ...\n{text[-half:]}"


def tail_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]
