"""Run engine: execution states, bounded reconciliation, canonical determinism."""

from typing import Any

import pytest

from agentchaos.engine import Engine, RunConfig, canonical_record
from agentchaos.results.states import Classification, ExecutionState

C = Classification


def lanes(engine: Engine, run_id: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"agent": [], "proxy": [], "ground_truth": []}
    for event in engine.store.events(run_id):
        out[event["lane"]].append(event)
    return out


async def test_fault_free_run_is_verified_from_database_state(engine: Engine) -> None:
    summary = await engine.run(RunConfig("baseline", seed=42))
    assert summary.state is ExecutionState.RESOLVED
    assert summary.classification is C.VERIFIED_RECOVERY
    assert summary.report.passed
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    assert outcome["agent_believed_outcome"] == "success"
    assert outcome["goal_achieved"] is True
    assert (outcome["duplicate_requests"], outcome["duplicate_committed_operations"]) == (0, 0)
    assert outcome["tool_calls"] == 5
    assert outcome["latency_ms"] == 100.0  # 5 calls x 20 logical ms
    by_lane = lanes(engine, summary.run_id)
    assert all(by_lane.values()), "all three timeline lanes have events"
    assert engine.store.state_history(summary.run_id) == [
        ExecutionState.RUNNING,
        ExecutionState.RESOLVED,
    ]


async def test_naive_retry_after_lost_response_double_invoices(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig("t", seed=42, fault_type="timeout_after_commit", target_step="create_invoice")
    )
    assert summary.classification is C.UNDETECTED_VIOLATION
    assert summary.report.violation_counts()["single_invoice_per_confirmed_order"] == 1
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    # The agent thinks everything went fine. The database disagrees.
    assert outcome["agent_believed_outcome"] == "success"
    assert (outcome["duplicate_requests"], outcome["duplicate_committed_operations"]) == (1, 1)
    agent = lanes(engine, summary.run_id)["agent"]
    retry = next(e for e in agent if e["kind"] == "decision")
    assert retry["payload"]["action"] == "retry"
    assert "assuming the call did not happen" in retry["payload"]["reason"]


async def test_duplicate_delivery_double_reserves_stock(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig("d", seed=42, fault_type="duplicate_delivery", target_step="reserve_inventory")
    )
    assert summary.classification is C.UNDETECTED_VIOLATION
    assert summary.report.violation_counts()["reserved_stock_matches_confirmed_orders"] == 1
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None and outcome["duplicate_committed_operations"] == 1
    # No retry happened: the agent never had a reason to suspect anything.
    assert not [e for e in lanes(engine, summary.run_id)["agent"] if e["kind"] == "decision"]


async def test_partial_commit_strands_earlier_steps(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig("p", seed=42, fault_type="partial_commit", target_step="create_invoice")
    )
    assert summary.classification is C.UNDETECTED_VIOLATION
    counts = summary.report.violation_counts()
    assert counts["no_orphaned_reservations"] == 1
    assert counts["reserved_stock_matches_confirmed_orders"] == 1
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    assert outcome["agent_believed_outcome"] == "failed"
    assert outcome["goal_achieved"] is False
    assert outcome["tool_calls"] == 5  # 2 ok + 3 failed attempts
    truth = engine.cluster.ground_truth()
    assert [o["status"] for o in truth.orders] == ["created"]
    assert [r["status"] for r in truth.reservations] == ["active"]


async def test_seed_picks_the_fault_target(engine: Engine) -> None:
    targets = set()
    for seed in range(42, 52):
        summary = await engine.run(RunConfig("s", seed=seed, fault_type="partial_commit"))
        targets.add(summary.target_step)
    assert targets == {"reserve_inventory", "create_invoice", "confirm_order"}


# --- determinism -----------------------------------------------------------


@pytest.mark.parametrize(
    ("fault_type", "controls"),
    [
        (None, ()),
        ("timeout_after_commit", ()),
        ("duplicate_delivery", ("idempotency",)),
        ("partial_commit", ("saga", "verify_after_write")),
        ("timeout_after_commit", ("idempotency", "saga", "verify_after_write")),
    ],
)
async def test_same_seed_and_config_give_identical_canonical_records(
    engine: Engine, fault_type: str | None, controls: tuple[str, ...]
) -> None:
    config = RunConfig("det", seed=7, fault_type=fault_type, controls=controls)
    first = await engine.run(config)
    second = await engine.run(config)
    assert first.run_id != second.run_id  # both are kept; results are append-only
    a, b = (
        canonical_record(engine.store, first.run_id),
        canonical_record(engine.store, second.run_id),
    )
    assert a == b
    assert len(a["events"]) > 10 and a["invariants"] and a["outcome"]["classification"]


async def test_different_seed_gives_a_different_canonical_record(engine: Engine) -> None:
    a = await engine.run(RunConfig("det", seed=7, fault_type="timeout_after_commit"))
    b = await engine.run(RunConfig("det", seed=8, fault_type="timeout_after_commit"))
    assert canonical_record(engine.store, a.run_id) != canonical_record(engine.store, b.run_id)


async def test_canonical_record_excludes_run_id_and_wall_clock(engine: Engine) -> None:
    summary = await engine.run(RunConfig("det", seed=7))
    text = repr(canonical_record(engine.store, summary.run_id))
    assert summary.run_id not in text
    assert "recorded_at" not in text


# --- reconciliation --------------------------------------------------------


async def test_async_commit_goes_through_pending_reconciliation_then_resolves(
    engine: Engine,
) -> None:
    summary = await engine.run(RunConfig("async", seed=42, async_delay_ms={"billing": 400}))
    assert engine.store.state_history(summary.run_id) == [
        ExecutionState.RUNNING,
        ExecutionState.PENDING_RECONCILIATION,
        ExecutionState.RESOLVED,
    ]
    attempts = engine.store.reconciliation_log(summary.run_id)
    assert [(a["attempt"], a["result"]) for a in attempts] == [(1, "settled")]
    # Verified only after the invoice really landed in the database.
    assert summary.classification is C.VERIFIED_RECOVERY
    assert len(engine.cluster.ground_truth().invoices) == 1


async def test_slow_async_commit_takes_several_logged_attempts(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig("async", seed=42, async_delay_ms={"billing": 1200}, reconcile_attempts=3)
    )
    attempts = engine.store.reconciliation_log(summary.run_id)
    assert [a["result"] for a in attempts] == ["still_pending", "still_pending", "settled"]
    assert [a["pending_operations"] for a in attempts] == [1, 1, 0]
    assert summary.state is ExecutionState.RESOLVED


async def test_unresolved_after_bounded_attempts_is_indeterminate_never_recovery(
    engine: Engine,
) -> None:
    summary = await engine.run(
        RunConfig(
            "async",
            seed=42,
            async_delay_ms={"billing": 60_000},
            reconcile_attempts=3,
            reconcile_interval_ms=500,
        )
    )
    assert summary.state is ExecutionState.INDETERMINATE
    assert summary.classification is None
    assert engine.store.state_history(summary.run_id)[-1] is ExecutionState.INDETERMINATE
    attempts = engine.store.reconciliation_log(summary.run_id)
    assert [a["result"] for a in attempts] == ["still_pending"] * 3
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None
    assert outcome["classification"] is None
    assert outcome["final_state"] == "indeterminate"
    assert outcome["goal_achieved"] is None
    assert "outcome unknown" in outcome["explanation"]
    # The agent was told 202 and believes it succeeded; that is not evidence.
    assert outcome["agent_believed_outcome"] == "success"


async def test_reconciliation_can_resolve_to_a_violation(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "async",
            seed=42,
            fault_type="timeout_after_commit",
            target_step="create_invoice",
            async_delay_ms={"billing": 400},
            reconcile_attempts=5,
        )
    )
    assert ExecutionState.PENDING_RECONCILIATION in engine.store.state_history(summary.run_id)
    assert summary.state is ExecutionState.RESOLVED
    assert summary.classification is C.UNDETECTED_VIOLATION
    assert len(engine.cluster.ground_truth().invoices) == 2


def test_config_rejects_unknown_or_conflicting_controls() -> None:
    with pytest.raises(ValueError):
        RunConfig("x", seed=1, controls=("prayer",))
    with pytest.raises(ValueError):
        RunConfig("x", seed=1, controls=("idempotency", "idempotency_client_only"))
