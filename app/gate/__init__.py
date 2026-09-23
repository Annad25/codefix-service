"""The delivery gate: an independent code path that decides what may be delivered.

It depends only on the standard library, the sandbox runner and the pure
archive-extraction function. It never touches agent state or worktrees.
"""
from .gate import DerivedChecks, GateResult, Tier, run_gate

__all__ = ["DerivedChecks", "GateResult", "Tier", "run_gate"]
