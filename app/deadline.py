"""Deadline propagation.

One Deadline is created when a request arrives and passed to everything that
can take time. Work asks it how long it may run instead of using fixed
timeouts, so nothing can outlive the request.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

Clock = Callable[[], float]


@dataclass
class Deadline:
    """An absolute point in monotonic time."""

    at: float
    clock: Clock = field(default=time.monotonic, repr=False)

    @classmethod
    def after(cls, seconds: float, clock: Clock = time.monotonic) -> "Deadline":
        return cls(at=clock() + seconds, clock=clock)

    def remaining(self) -> float:
        return max(0.0, self.at - self.clock())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def timeout(self, cap: float | None = None) -> float:
        """Seconds a single operation may take: the remaining time, optionally capped."""
        left = self.remaining()
        return left if cap is None else min(cap, left)

    def shortened(self, reserve: float) -> "Deadline":
        """A child deadline that ends `reserve` seconds earlier (e.g. to keep time for the gate)."""
        return Deadline(at=self.at - reserve, clock=self.clock)

    @classmethod
    def for_request(cls, deadline_seconds: float, *, margin_floor: float = 10.0,
                    margin_fraction: float = 0.07, started_at: float | None = None,
                    clock: Clock = time.monotonic) -> "Deadline":
        """The hard deadline for a request, keeping a margin for upload and response transfer."""
        margin = max(margin_floor, margin_fraction * deadline_seconds)
        started = clock() if started_at is None else started_at
        return cls(at=started + max(0.0, deadline_seconds - margin), clock=clock)
