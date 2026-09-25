"""Per-operation time limit shared by every blocking I/O step of that operation."""

from __future__ import annotations

import time

from lab_scopes.errors import ScopeTimeoutError


class Deadline:
    """Monotonic end time."""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self._end = time.monotonic() + self.seconds

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def clamp(self, step_timeout: float) -> float:
        """``step_timeout`` limited to the time left; ScopeTimeoutError when none is left."""
        left = self.remaining()
        if left <= 0:
            raise ScopeTimeoutError(f"deadline of {self.seconds:g} s exceeded")
        return min(step_timeout, left)

    def sleep(self, seconds: float) -> None:
        """Sleep the full ``seconds`` (a settle delay or backoff is never shortened), or raise
        ScopeTimeoutError if less time is left."""
        if self.remaining() < seconds:
            raise ScopeTimeoutError(f"deadline of {self.seconds:g} s leaves no time for a {seconds:g} s wait")
        time.sleep(seconds)


def clamp(deadline: Deadline | None, step_timeout: float) -> float:
    """``Deadline.clamp``; ``None`` means no overall limit."""
    return step_timeout if deadline is None else deadline.clamp(step_timeout)


def sleep(deadline: Deadline | None, seconds: float) -> None:
    """``Deadline.sleep``; ``None`` means no overall limit."""
    if deadline is None:
        time.sleep(seconds)
    else:
        deadline.sleep(seconds)
