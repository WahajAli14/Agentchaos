"""Ground-truth state and invariant result types.

`GroundTruth` is read straight from the mock services' databases. API responses
are observations; this is what actually happened.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

Row = dict[str, Any]


@dataclass(frozen=True)
class GroundTruth:
    orders: Sequence[Row] = ()
    stock: Sequence[Row] = ()
    reservations: Sequence[Row] = ()
    invoices: Sequence[Row] = ()
    notifications: Sequence[Row] = ()
    # Writes accepted by a service but not yet committed. While this is non-zero
    # the final state is not knowable and invariant results are provisional.
    pending_async_operations: int = 0


@dataclass(frozen=True)
class Violation:
    invariant: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvariantResult:
    invariant: str
    violations: tuple[Violation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations


@runtime_checkable
class Invariant(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult: ...


@dataclass(frozen=True)
class CheckReport:
    checked_at_ms: int
    results: tuple[InvariantResult, ...]
    pending_async_operations: int = 0

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def violations(self) -> tuple[Violation, ...]:
        return tuple(v for result in self.results for v in result.violations)

    def violation_counts(self) -> dict[str, int]:
        return {result.invariant: len(result.violations) for result in self.results}


class InvariantChecker:
    def __init__(self, invariants: Sequence[Invariant]) -> None:
        self.invariants = tuple(invariants)

    def check(self, truth: GroundTruth, now_ms: int) -> CheckReport:
        return CheckReport(
            checked_at_ms=now_ms,
            results=tuple(inv.check(truth, now_ms) for inv in self.invariants),
            pending_async_operations=truth.pending_async_operations,
        )
