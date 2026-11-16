"""Controllable logical clock.

Services, proxy and engine never read wall-clock time. Time is an integer number
of logical milliseconds since the start of a run, and only moves when something
explicitly advances it.
"""

from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class ManualClock:
    def __init__(self, start_ms: int = 0) -> None:
        self._start_ms = start_ms
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> int:
        if ms < 0:
            raise ValueError("clock cannot move backwards")
        self._now_ms += ms
        return self._now_ms

    def set(self, ms: int) -> int:
        if ms < self._now_ms:
            raise ValueError("clock cannot move backwards")
        self._now_ms = ms
        return self._now_ms

    def reset(self) -> None:
        self._now_ms = self._start_ms
