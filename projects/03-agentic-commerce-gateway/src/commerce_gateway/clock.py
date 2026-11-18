"""Injected clocks. Time is integer milliseconds since the Unix epoch, UTC (ADR 0004).

`SystemClock` is the only place in the package that reads the real time. Entry points always use
it; only tests construct a `FixedClock`.
"""

import time
from typing import Protocol


def seconds_left(expires_at_ms: int, now_ms: int) -> int:
    """Whole seconds until an expiry, 0 once expired (`now >= expires_at_ms`), never negative."""
    return max(0, (expires_at_ms - now_ms) // 1000)


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    """The real wall clock."""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class FixedClock:
    """A clock that only moves when a test moves it."""

    def __init__(self, start_ms: int) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("advance takes a non-negative duration; use set() to go back")
        self._now_ms += ms

    def set(self, ms: int) -> None:
        """Set the reading to any value, including an earlier one (a clock moved backwards)."""
        self._now_ms = ms
