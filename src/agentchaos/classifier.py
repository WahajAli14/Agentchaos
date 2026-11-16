"""Final classification of a resolved run, from verified state only.

Inputs are all ground truth: the invariant report (service databases), whether
the order really ended confirmed, how many duplicate operations really
committed, and whether a control produced a state-verified escalation.

    invariants violated, verified escalation   -> manual_intervention_required
    invariants violated, nobody noticed        -> undetected_violation
    invariants hold, goal reached, no leftovers-> verified_recovery
    invariants hold, verified escalation       -> manual_intervention_required
    invariants hold otherwise                  -> partial_recovery

`partial_recovery` means the databases are consistent but the outcome is not
the intended one: the order was not fulfilled, or it was fulfilled with a
duplicate side effect left behind. `manual_intervention_required` is never a
success.
"""

from agentchaos.invariants.base import CheckReport
from agentchaos.results.states import Classification


def classify(
    report: CheckReport,
    *,
    goal_achieved: bool,
    duplicate_committed_operations: int,
    verified_escalation: bool,
) -> tuple[Classification, str]:
    if not report.passed:
        counts = {name: n for name, n in report.violation_counts().items() if n}
        detail = ", ".join(f"{name} x{n}" for name, n in sorted(counts.items()))
        if verified_escalation:
            return (
                Classification.MANUAL_INTERVENTION_REQUIRED,
                f"Invariants violated ({detail}); a control verified the state and escalated.",
            )
        return (
            Classification.UNDETECTED_VIOLATION,
            f"Invariants violated ({detail}) and nothing in the run detected it.",
        )
    if goal_achieved and duplicate_committed_operations == 0:
        return Classification.VERIFIED_RECOVERY, "Order confirmed; every invariant holds."
    if verified_escalation:
        return (
            Classification.MANUAL_INTERVENTION_REQUIRED,
            "Invariants hold, but the workflow failed with committed writes left over; "
            "a control verified them and escalated.",
        )
    if goal_achieved:
        return (
            Classification.PARTIAL_RECOVERY,
            f"Order confirmed and invariants hold, but {duplicate_committed_operations} "
            "duplicate operation(s) committed and were left behind.",
        )
    return (
        Classification.PARTIAL_RECOVERY,
        "Invariants hold, but the order was not fulfilled.",
    )
