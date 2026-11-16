"""Safety controls: the same seeded scenario with and without each control."""

from pathlib import Path
from typing import Any

import pytest

from agentchaos.controls import DedupeStore, IdempotencyControl, idempotency_key
from agentchaos.engine import Engine, RunConfig
from agentchaos.interfaces import ProxyRequest, ProxyResponse, SafetyControl, StepRequest
from agentchaos.proxy import Recorder
from agentchaos.results.states import Classification
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

V = Classification.VERIFIED_RECOVERY
P = Classification.PARTIAL_RECOVERY
M = Classification.MANUAL_INTERVENTION_REQUIRED
U = Classification.UNDETECTED_VIOLATION
ALL = ("idempotency", "saga", "verify_after_write")

# (fault, step it hits, controls, expected classification)
MATRIX = [
    # Lost response on a non-idempotent create: fixed by server-honoured keys or
    # by checking state before retrying. Not by keys the server ignores, not by saga.
    ("timeout_after_commit", "create_invoice", (), U),
    ("timeout_after_commit", "create_invoice", ("idempotency_client_only",), U),
    ("timeout_after_commit", "create_invoice", ("saga",), U),
    ("timeout_after_commit", "create_invoice", ("idempotency",), V),
    ("timeout_after_commit", "create_invoice", ("verify_after_write",), V),
    ("timeout_after_commit", "create_invoice", ALL, V),
    ("timeout_after_commit", "reserve_inventory", (), U),
    ("timeout_after_commit", "reserve_inventory", ALL, V),
    # A stray duplicate that breaks no invariant is still not a clean result.
    ("timeout_after_commit", "create_order", (), P),
    ("timeout_after_commit", "notify_customer", (), P),
    ("timeout_after_commit", "create_order", ALL, V),
    # Confirm is naturally idempotent: the retry reaches the service but changes nothing.
    ("timeout_after_commit", "confirm_order", (), V),
    # Duplicate hidden behind a normal 2xx: only server-side idempotency sees it.
    ("duplicate_delivery", "reserve_inventory", (), U),
    ("duplicate_delivery", "reserve_inventory", ("verify_after_write",), U),
    ("duplicate_delivery", "reserve_inventory", ("saga",), U),
    ("duplicate_delivery", "reserve_inventory", ("idempotency_client_only",), U),
    ("duplicate_delivery", "reserve_inventory", ("idempotency",), V),
    ("duplicate_delivery", "create_invoice", (), U),
    ("duplicate_delivery", "create_invoice", ALL, V),
    # Mid-workflow outage: nothing can complete the order. Saga leaves consistent
    # state; verify-after-write alone can only escalate with verified facts.
    ("partial_commit", "create_invoice", (), U),
    ("partial_commit", "create_invoice", ("idempotency",), U),
    ("partial_commit", "create_invoice", ("verify_after_write",), M),
    ("partial_commit", "create_invoice", ("saga",), P),
    ("partial_commit", "create_invoice", ALL, P),
    ("partial_commit", "confirm_order", (), U),
    ("partial_commit", "confirm_order", ("saga",), P),
    ("partial_commit", "reserve_inventory", (), P),
    ("partial_commit", "reserve_inventory", ("verify_after_write",), M),
    ("partial_commit", "reserve_inventory", ("saga",), P),
]


@pytest.mark.parametrize(("fault", "step", "controls", "expected"), MATRIX)
async def test_before_and_after(
    engine: Engine, fault: str, step: str, controls: tuple[str, ...], expected: Classification
) -> None:
    summary = await engine.run(
        RunConfig("matrix", seed=42, fault_type=fault, target_step=step, controls=controls)
    )
    assert summary.classification is expected, summary.explanation
    # Whatever the label, it has to agree with the databases.
    if expected in (V, P):
        assert summary.report.passed
    if expected is U:
        assert not summary.report.passed
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    if expected is V:
        assert outcome["goal_achieved"] is True
        assert outcome["duplicate_committed_operations"] == 0


def events(engine: Engine, run_id: str, kind: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in engine.store.events(run_id) if e["kind"] == kind]


async def test_server_idempotency_still_receives_the_duplicate_request(engine: Engine) -> None:
    """Duplicate requests and duplicate committed operations are different numbers."""
    summary = await engine.run(
        RunConfig(
            "idem",
            seed=42,
            fault_type="timeout_after_commit",
            target_step="create_invoice",
            controls=("idempotency",),
        )
    )
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    assert (outcome["duplicate_requests"], outcome["duplicate_committed_operations"]) == (1, 0)
    replays = [e for e in events(engine, summary.run_id, "delivered") if e["idempotent_replay"]]
    assert len(replays) == 1
    assert len(engine.cluster.ground_truth().invoices) == 1


async def test_verify_after_write_checks_state_instead_of_retrying(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "verify",
            seed=42,
            fault_type="timeout_after_commit",
            target_step="create_invoice",
            controls=("verify_after_write",),
        )
    )
    checks = events(engine, summary.run_id, "control_verify")
    assert [(c["step"], c["committed"]) for c in checks] == [("create_invoice", True)]
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    assert (outcome["duplicate_requests"], outcome["duplicate_committed_operations"]) == (0, 0)
    assert not events(engine, summary.run_id, "decision")  # no retry was needed
    invoice = engine.cluster.ground_truth().invoices[0]
    assert engine.cluster.ground_truth().orders[0]["invoice_id"] == invoice["id"]


async def test_verify_after_write_lets_a_real_failure_retry(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "verify",
            seed=42,
            fault_type="partial_commit",
            target_step="create_invoice",
            controls=("verify_after_write",),
        )
    )
    checks = [c for c in events(engine, summary.run_id, "control_verify")]
    assert [c["committed"] for c in checks] == [False, False, False]
    escalation = events(engine, summary.run_id, "escalation")
    assert len(escalation) == 1
    assert escalation[0]["verified"] is True
    assert [item["step"] for item in escalation[0]["leftover"]] == [
        "create_order",
        "reserve_inventory",
    ]
    # Escalating is not recovering: the databases are still wrong.
    assert summary.classification is M
    assert not summary.report.passed


async def test_saga_compensates_but_does_not_roll_back(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "saga",
            seed=42,
            fault_type="partial_commit",
            target_step="confirm_order",
            controls=("saga",),
        )
    )
    compensations = events(engine, summary.run_id, "control_compensation")
    assert [c["step"] for c in compensations] == [
        "create_invoice",
        "reserve_inventory",
        "create_order",
    ]
    assert all(c["verified_undone"] for c in compensations)
    assert all("not a rollback" in c["note"] for c in compensations)
    truth = engine.cluster.ground_truth()
    # The rows are still there, marked undone. Nothing was erased.
    assert [i["status"] for i in truth.invoices] == ["void"]
    assert [r["status"] for r in truth.reservations] == ["released"]
    assert [o["status"] for o in truth.orders] == ["cancelled"]
    assert all(s["reserved"] == 0 for s in truth.stock)
    assert summary.classification is P
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None and outcome["goal_achieved"] is False


async def test_saga_cannot_compensate_writes_the_agent_never_saw(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "saga",
            seed=42,
            fault_type="duplicate_delivery",
            target_step="create_invoice",
            controls=("saga",),
        )
    )
    assert summary.classification is U
    assert not events(engine, summary.run_id, "control_compensation")


# --- units -----------------------------------------------------------------


def test_idempotency_key_is_stable_per_operation() -> None:
    assert idempotency_key("wf:42", "create_invoice") == idempotency_key("wf:42", "create_invoice")
    assert idempotency_key("wf:42", "create_invoice") != idempotency_key("wf:43", "create_invoice")
    assert idempotency_key("wf:42", "create_invoice") != idempotency_key("wf:42", "create_order")


def test_dedupe_store_survives_reopen_and_resets(tmp_path: Path) -> None:
    path = tmp_path / "dedupe.db"
    store = DedupeStore(path)
    assert store.get("k") is None
    store.put("k", ProxyResponse(201, {"id": "inv_1"}))
    store.close()
    reopened = DedupeStore(path)
    hit = reopened.get("k")
    assert hit is not None
    assert (hit.status_code, hit.body, hit.source) == (201, {"id": "inv_1"}, "dedupe_store")
    reopened.reset()
    assert reopened.get("k") is None
    reopened.close()


async def test_client_dedupe_answers_a_repeated_completed_operation(
    cluster: ServiceCluster, store: ResultsStore, tmp_path: Path
) -> None:
    store.create_run(
        "run_1",
        scenario="u",
        workflow="w",
        mode="deterministic",
        seed=1,
        fault_type=None,
        controls=["idempotency_client_only"],
        logical_ms=0,
    )
    dedupe = DedupeStore(tmp_path / "d.db")
    control = IdempotencyControl(
        "idempotency_client_only", "w:1", dedupe, Recorder(store, "run_1", cluster)
    )
    assert isinstance(control, SafetyControl)
    seen: list[ProxyRequest] = []
    answers = [ProxyResponse(None, None, timed_out=True), ProxyResponse(201, {"id": "inv_1"})]

    async def downstream(request: ProxyRequest) -> ProxyResponse:
        seen.append(request)
        return answers[len(seen) - 1]

    request = ProxyRequest(
        "run_1", "create_invoice", 1, StepRequest("billing", "POST", "/invoices")
    )
    lost = await control.around_call(request, downstream)
    assert lost.timed_out  # nothing to remember: this is why the client alone is not enough
    first = await control.around_call(request, downstream)
    again = await control.around_call(request, downstream)
    assert len(seen) == 2  # third call never left the client
    assert first.body == again.body and again.source == "dedupe_store"
    assert {r.headers["Idempotency-Key"] for r in seen} == {
        idempotency_key("w:1", "create_invoice")
    }

    read = ProxyRequest("run_1", "create_invoice", 1, StepRequest("billing", "GET", "/invoices"))
    answers.append(ProxyResponse(200, []))
    await control.around_call(read, downstream)
    assert "Idempotency-Key" not in seen[-1].headers
    dedupe.close()
