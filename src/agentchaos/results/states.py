"""Execution state and final classification are separate things.

A run moves through execution states. Only a `resolved` run gets a
classification. `indeterminate` is its own reported category and can never be
turned into a recovery by waiting.
"""

from enum import StrEnum


class ExecutionState(StrEnum):
    RUNNING = "running"
    PENDING_RECONCILIATION = "pending_reconciliation"
    RESOLVED = "resolved"
    INDETERMINATE = "indeterminate"


class Classification(StrEnum):
    VERIFIED_RECOVERY = "verified_recovery"
    PARTIAL_RECOVERY = "partial_recovery"
    # A verified escalation decision. Never a success in any metric.
    MANUAL_INTERVENTION_REQUIRED = "manual_intervention_required"
    UNDETECTED_VIOLATION = "undetected_violation"


ALLOWED_TRANSITIONS: dict[ExecutionState | None, frozenset[ExecutionState]] = {
    None: frozenset({ExecutionState.RUNNING}),
    ExecutionState.RUNNING: frozenset(
        {ExecutionState.PENDING_RECONCILIATION, ExecutionState.RESOLVED}
    ),
    ExecutionState.PENDING_RECONCILIATION: frozenset(
        {ExecutionState.RESOLVED, ExecutionState.INDETERMINATE}
    ),
    ExecutionState.RESOLVED: frozenset(),
    ExecutionState.INDETERMINATE: frozenset(),
}


class InvalidTransition(Exception):
    pass


class InvalidOutcome(Exception):
    pass
