import asyncio
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from agentchaos.classifier import classify
from agentchaos.controls import CONTROL_NAMES
from agentchaos.engine import Engine, RunConfig
from agentchaos.faults import ELIGIBLE_STEPS, FAULT_TYPES
from agentchaos.invariants import CheckReport, InvariantChecker, InvariantResult, Violation
from agentchaos.results.states import Classification, ExecutionState
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

C = Classification
PASS = CheckReport(0, (InvariantResult("a"),))
FAIL = CheckReport(0, (InvariantResult("a", (Violation("a", "order", "o1"),)),))


def label(report: CheckReport, goal: bool, dups: int, escalated: bool) -> Classification:
    return classify(
        report,
        goal_achieved=goal,
        duplicate_committed_operations=dups,
        verified_escalation=escalated,
    )[0]


def test_decision_table() -> None:
    assert label(PASS, True, 0, False) is C.VERIFIED_RECOVERY
    assert label(PASS, True, 1, False) is C.PARTIAL_RECOVERY
    assert label(PASS, False, 0, False) is C.PARTIAL_RECOVERY
    assert label(PASS, False, 0, True) is C.MANUAL_INTERVENTION_REQUIRED
    assert label(FAIL, True, 0, False) is C.UNDETECTED_VIOLATION
    assert label(FAIL, False, 0, False) is C.UNDETECTED_VIOLATION
    assert label(FAIL, False, 0, True) is C.MANUAL_INTERVENTION_REQUIRED
    assert label(FAIL, True, 0, True) is C.MANUAL_INTERVENTION_REQUIRED


def test_explanations_name_the_violated_invariants() -> None:
    _, text = classify(
        FAIL, goal_achieved=True, duplicate_committed_operations=0, verified_escalation=False
    )
    assert "a x1" in text


@given(
    violated=st.booleans(),
    goal=st.booleans(),
    dups=st.integers(0, 5),
    escalated=st.booleans(),
)
def test_verified_recovery_only_with_clean_verified_state(
    violated: bool, goal: bool, dups: int, escalated: bool
) -> None:
    result = label(FAIL if violated else PASS, goal, dups, escalated)
    if violated:
        assert result in (C.UNDETECTED_VIOLATION, C.MANUAL_INTERVENTION_REQUIRED)
    if result is C.VERIFIED_RECOVERY:
        assert not violated and goal and dups == 0


CONTROL_SETS = st.sets(st.sampled_from(CONTROL_NAMES)).filter(
    lambda s: not {"idempotency", "idempotency_client_only"} <= s
)


@settings(max_examples=40, deadline=None)
@given(
    fault=st.sampled_from(FAULT_TYPES),
    data=st.data(),
    controls=CONTROL_SETS,
    seed=st.integers(0, 10_000),
    async_billing=st.sampled_from([0, 300, 90_000]),
)
def test_random_fault_runs_never_claim_recovery_over_violated_state(
    fault: str, data: st.DataObject, controls: set[str], seed: int, async_billing: int
) -> None:
    step = data.draw(st.sampled_from(ELIGIBLE_STEPS[fault]))
    with tempfile.TemporaryDirectory() as tmp:
        cluster = ServiceCluster(Path(tmp) / "services", seed=seed)
        store = ResultsStore(Path(tmp) / "results" / "results.db")
        engine = Engine(cluster, store, Path(tmp) / "control")
        try:
            summary = asyncio.run(
                engine.run(
                    RunConfig(
                        "property",
                        seed=seed,
                        fault_type=fault,
                        target_step=step,
                        controls=tuple(sorted(controls)),
                        async_delay_ms={"billing": async_billing},
                    )
                )
            )
            # Re-read the databases independently of what the engine stored.
            truth = cluster.ground_truth()
            recheck = InvariantChecker(engine.spec.invariants()).check(
                truth, cluster.runtime.clock.now_ms()
            )
            if summary.state is ExecutionState.INDETERMINATE:
                assert summary.classification is None
                assert truth.pending_async_operations > 0
            else:
                assert truth.pending_async_operations == 0
                if not recheck.passed:
                    assert summary.classification is not C.VERIFIED_RECOVERY
                    assert summary.classification is not C.PARTIAL_RECOVERY
                if summary.classification is C.VERIFIED_RECOVERY:
                    assert recheck.passed
                    assert [o["status"] for o in truth.orders] == ["confirmed"]
                    assert len([i for i in truth.invoices if i["status"] == "issued"]) == 1
        finally:
            engine.close()
            cluster.close()
            store.close()
