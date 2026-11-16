"""Property: whatever sequence of extra writes hits the services, the checker
agrees with an independent model of the state, and a violated run can never be
stored as `verified_recovery`."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agentchaos.invariants import InvariantChecker
from agentchaos.results.states import Classification, ExecutionState, InvalidOutcome
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster
from agentchaos.workflows.order import DEFAULT_ORDER_INPUT, OrderWorkflow
from tests.conftest import run_workflow

OPS = ("add_invoice", "void_invoice", "add_reservation", "release_reservation")


async def apply(cluster: ServiceCluster, ops: list[str]) -> tuple[int, int]:
    """Happy path, then `ops`. Returns the model's (issued invoices, active reservations)."""
    spec = OrderWorkflow()
    async with cluster.client() as client:
        ctx = await run_workflow(spec, client, DEFAULT_ORDER_INPUT)
        invoices, reservations = [ctx["invoice_id"]], [ctx["reservation_id"]]
        for op in ops:
            if op == "add_invoice":
                r = await client.post(
                    "http://billing/invoices",
                    json={"order_id": ctx["order_id"], "amount_cents": 7500},
                )
                invoices.append(r.json()["id"])
            elif op == "void_invoice" and invoices:
                await client.post(f"http://billing/invoices/{invoices.pop()}/void")
            elif op == "add_reservation":
                r = await client.post(
                    "http://inventory/reservations",
                    json={"order_id": ctx["order_id"], "sku": ctx["sku"], "quantity": 3},
                )
                reservations.append(r.json()["id"])
            elif op == "release_reservation" and reservations:
                await client.post(f"http://inventory/reservations/{reservations.pop()}/release")
    return len(invoices), len(reservations)


@settings(max_examples=40, deadline=None)
@given(ops=st.lists(st.sampled_from(OPS), max_size=8), seed=st.integers(0, 2**16))
def test_violated_state_is_always_detected_and_never_verified(ops: list[str], seed: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = ServiceCluster(Path(tmp) / "services", seed=seed)
        store = ResultsStore(Path(tmp) / "results" / "results.db")
        try:
            cluster.reset(seed)
            issued, active = asyncio.run(apply(cluster, ops))
            report = InvariantChecker(OrderWorkflow().invariants()).check(
                cluster.ground_truth(), cluster.runtime.clock.now_ms()
            )
            assert report.passed == (issued == 1 and active == 1)

            store.create_run(
                "run",
                scenario="property",
                workflow="order_fulfilment",
                mode="deterministic",
                seed=seed,
                fault_type=None,
                controls=[],
                logical_ms=0,
            )
            store.record_invariant_report("run", report)
            store.transition("run", ExecutionState.RESOLVED, 1, "done")
            if report.passed:
                store.record_outcome(
                    "run", classification=Classification.VERIFIED_RECOVERY, explanation="ok"
                )
            else:
                with pytest.raises(InvalidOutcome):
                    store.record_outcome(
                        "run", classification=Classification.VERIFIED_RECOVERY, explanation="no"
                    )
                assert store.outcome("run") is None
        finally:
            cluster.close()
            store.close()
