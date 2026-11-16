import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from agentchaos.invariants import CheckReport, InvariantResult, Violation
from agentchaos.results.states import (
    Classification,
    ExecutionState,
    InvalidOutcome,
    InvalidTransition,
)
from agentchaos.results.store import ResultsStore, RunMetrics, metadata

PASS = CheckReport(0, (InvariantResult("a"), InvariantResult("b")))
FAIL = CheckReport(
    0, (InvariantResult("a"), InvariantResult("b", (Violation("b", "order", "o1", {"n": 2}),)))
)


def new_run(store: ResultsStore, run_id: str = "run_1") -> str:
    store.create_run(
        run_id,
        scenario="timeout_after_commit",
        workflow="order_fulfilment",
        mode="deterministic",
        seed=42,
        fault_type="timeout_after_commit",
        controls=["verify_after_write", "idempotency"],
        logical_ms=0,
    )
    return run_id


def test_run_round_trip(store: ResultsStore) -> None:
    run_id = new_run(store)
    run = store.get_run(run_id)
    assert run is not None
    assert run["controls"] == ["idempotency", "verify_after_write"]
    assert store.current_state(run_id) is ExecutionState.RUNNING
    assert store.append_event(run_id, lane="agent", kind="call", logical_ms=1, payload={}) == 0
    assert store.append_event(run_id, lane="proxy", kind="fault", logical_ms=2, payload={}) == 1
    assert [e["lane"] for e in store.events(run_id)] == ["agent", "proxy"]


@pytest.mark.parametrize("table", [t.name for t in metadata.sorted_tables])
def test_every_table_rejects_update_and_delete(store: ResultsStore, table: str) -> None:
    run_id = new_run(store)
    store.append_event(run_id, lane="agent", kind="call", logical_ms=1, payload={})
    store.record_reconciliation_attempt(
        run_id, attempt=1, logical_ms=5, pending_operations=0, result="settled"
    )
    store.record_invariant_report(run_id, PASS)
    store.transition(run_id, ExecutionState.RESOLVED, 9, "done")
    store.record_outcome(
        run_id, classification=Classification.VERIFIED_RECOVERY, explanation="all invariants hold"
    )
    with store.engine.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() >= 1
    for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET run_id = 'tampered'"):
        with pytest.raises(DBAPIError, match="append-only"), store.engine.begin() as conn:
            conn.execute(text(statement))
    assert store.get_run(run_id) is not None


def test_reopening_the_store_keeps_history(store: ResultsStore) -> None:
    new_run(store)
    reopened = ResultsStore(store.db_path)
    assert reopened.count_runs() == 1
    new_run(reopened, "run_2")
    assert store.count_runs() == 2
    reopened.close()


def test_only_allowed_state_transitions(store: ResultsStore) -> None:
    run_id = new_run(store)
    with pytest.raises(InvalidTransition):
        store.transition(run_id, ExecutionState.INDETERMINATE, 1, "skip reconciliation")
    with pytest.raises(InvalidTransition):
        store.transition(run_id, ExecutionState.RUNNING, 1, "again")
    store.transition(run_id, ExecutionState.PENDING_RECONCILIATION, 1, "async op outstanding")
    store.transition(run_id, ExecutionState.INDETERMINATE, 2, "still unknown after 3 attempts")
    with pytest.raises(InvalidTransition):
        store.transition(run_id, ExecutionState.RESOLVED, 3, "timeout")
    assert store.state_history(run_id) == [
        ExecutionState.RUNNING,
        ExecutionState.PENDING_RECONCILIATION,
        ExecutionState.INDETERMINATE,
    ]


def test_indeterminate_run_can_never_be_classified(store: ResultsStore) -> None:
    run_id = new_run(store)
    store.transition(run_id, ExecutionState.PENDING_RECONCILIATION, 1, "async op outstanding")
    store.record_reconciliation_attempt(
        run_id, attempt=1, logical_ms=2, pending_operations=1, result="still_pending"
    )
    store.transition(run_id, ExecutionState.INDETERMINATE, 3, "attempts exhausted")
    store.record_invariant_report(run_id, PASS)
    for classification in Classification:
        with pytest.raises(InvalidOutcome):
            store.record_outcome(run_id, classification=classification, explanation="x")
    store.record_outcome(run_id, classification=None, explanation="escalated: outcome unknown")
    outcome = store.outcome(run_id)
    assert outcome is not None
    assert (outcome["classification"], outcome["final_state"]) == (None, "indeterminate")
    assert [a["result"] for a in store.reconciliation_log(run_id)] == ["still_pending"]


def test_unfinished_run_cannot_get_an_outcome(store: ResultsStore) -> None:
    run_id = new_run(store)
    store.record_invariant_report(run_id, PASS)
    with pytest.raises(InvalidOutcome):
        store.record_outcome(
            run_id, classification=Classification.VERIFIED_RECOVERY, explanation=""
        )
    store.transition(run_id, ExecutionState.PENDING_RECONCILIATION, 1, "waiting")
    with pytest.raises(InvalidOutcome):
        store.record_outcome(
            run_id, classification=Classification.VERIFIED_RECOVERY, explanation=""
        )


def test_verified_recovery_needs_stored_passing_invariants(store: ResultsStore) -> None:
    unchecked = new_run(store, "unchecked")
    store.transition(unchecked, ExecutionState.RESOLVED, 1, "done")
    with pytest.raises(InvalidOutcome, match="no stored invariant results"):
        store.record_outcome(
            unchecked, classification=Classification.VERIFIED_RECOVERY, explanation=""
        )

    violated = new_run(store, "violated")
    store.record_invariant_report(violated, FAIL)
    store.transition(violated, ExecutionState.RESOLVED, 1, "done")
    with pytest.raises(InvalidOutcome, match="invariant violations"):
        store.record_outcome(
            violated, classification=Classification.VERIFIED_RECOVERY, explanation=""
        )
    with pytest.raises(InvalidOutcome):
        store.record_outcome(violated, classification=None, explanation="")
    store.record_outcome(
        violated,
        classification=Classification.UNDETECTED_VIOLATION,
        explanation="two invoices for o1",
        metrics=RunMetrics(tool_calls=5, duplicate_requests=1, duplicate_committed_operations=1),
    )
    outcome = store.outcome(violated)
    assert outcome is not None
    assert outcome["duplicate_requests"] == 1
    assert outcome["duplicate_committed_operations"] == 1
    assert outcome["billed_cost_usd"] == 0.0
    assert outcome["estimated_cost_usd"] is None  # unknown, not guessed
    stored = store.invariant_results(violated)
    assert [(r["invariant"], r["passed"]) for r in stored] == [("a", True), ("b", False)]
    assert stored[1]["violations"][0]["entity_id"] == "o1"
