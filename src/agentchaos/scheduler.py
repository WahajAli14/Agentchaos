"""Deterministic event scheduler for async processing in the mock services.

Events fire only when logical time is advanced past their due time, in
(due time, scheduling order). There are no threads, timers or background tasks.
"""

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field

from agentchaos.clock import ManualClock


@dataclass(order=True)
class ScheduledEvent:
    due_ms: int
    seq: int
    name: str = field(compare=False)
    callback: Callable[[], None] = field(compare=False, repr=False)


class DeterministicScheduler:
    def __init__(self, clock: ManualClock) -> None:
        self._clock = clock
        self._heap: list[ScheduledEvent] = []
        self._seq = 0

    def schedule(self, delay_ms: int, name: str, callback: Callable[[], None]) -> ScheduledEvent:
        if delay_ms < 0:
            raise ValueError("delay must be >= 0")
        event = ScheduledEvent(self._clock.now_ms() + delay_ms, self._seq, name, callback)
        self._seq += 1
        heapq.heappush(self._heap, event)
        return event

    def pending(self) -> list[ScheduledEvent]:
        return sorted(self._heap)

    def run_due(self) -> list[str]:
        """Run every event due at or before the current logical time."""
        return self.advance(0)

    def advance(self, ms: int) -> list[str]:
        """Advance logical time by `ms`, firing events at their own due time."""
        if ms < 0:
            raise ValueError("clock cannot move backwards")
        target = self._clock.now_ms() + ms
        fired: list[str] = []
        while self._heap and self._heap[0].due_ms <= target:
            event = heapq.heappop(self._heap)
            self._clock.set(max(self._clock.now_ms(), event.due_ms))
            event.callback()
            fired.append(event.name)
        self._clock.set(target)
        return fired

    def clear(self) -> None:
        self._heap.clear()
        self._seq = 0
