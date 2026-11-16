"""Each invariant: passes on correct state, fails on the specific broken state."""

from typing import Any

import httpx

from agentchaos.interfaces import Invariant, WorkflowSpec
from agentchaos.invariants import GroundTruth, InvariantChecker
from agentchaos.invariants.order import (
    ConfirmedOrderHasInvoice,
    InvoiceHasReservation,
    NoOrphanedReservations,
    ReservedStockMatchesConfirmedOrders,
    SingleInvoicePerConfirmedOrder,
    order_invariants,
)
from agentchaos.services.cluster import ServiceCluster
from agentchaos.workflows.order import DEFAULT_ORDER_INPUT, OrderWorkflow
from tests.conftest import run_workflow

Row = dict[str, Any]


def order(id: str = "o1", status: str = "confirmed", sku: str = "W", quantity: int = 3) -> Row:
    return {"id": id, "status": status, "sku": sku, "quantity": quantity}


def reservation(
    id: str = "r1", order_id: str = "o1", status: str = "active", created_at_ms: int = 0
) -> Row:
    return {
        "id": id,
        "order_id": order_id,
        "sku": "W",
        "quantity": 3,
        "status": status,
        "created_at_ms": created_at_ms,
    }


def invoice(id: str = "i1", order_id: str = "o1", status: str = "issued") -> Row:
    return {"id": id, "order_id": order_id, "status": status}


def stock(reserved: int) -> Row:
    return {"sku": "W", "on_hand": 100, "reserved": reserved}


GOOD = GroundTruth(
    orders=[order()], stock=[stock(3)], reservations=[reservation()], invoices=[invoice()]
)


def test_correct_state_passes_every_invariant() -> None:
    report = InvariantChecker(order_invariants()).check(GOOD, now_ms=10**9)
    assert report.passed
    assert report.violations == ()
    assert set(report.violation_counts().values()) == {0}
    assert len(report.results) == 5


def test_empty_state_passes() -> None:
    assert InvariantChecker(order_invariants()).check(GroundTruth(), now_ms=0).passed


def test_invariants_satisfy_the_interface() -> None:
    assert all(isinstance(inv, Invariant) for inv in order_invariants())
    assert isinstance(OrderWorkflow(), WorkflowSpec)


def test_duplicate_invoice_for_confirmed_order() -> None:
    inv = SingleInvoicePerConfirmedOrder()
    truth = GroundTruth(orders=[order()], invoices=[invoice("i1"), invoice("i2")])
    result = inv.check(truth, 0)
    assert not result.passed
    assert result.violations[0].entity_id == "o1"
    assert result.violations[0].detail == {"issued_invoice_ids": ["i1", "i2"]}
    # A voided duplicate is history, not a current violation.
    voided = GroundTruth(orders=[order()], invoices=[invoice("i1"), invoice("i2", status="void")])
    assert inv.check(voided, 0).passed
    # Missing invoice is the other invariant's job, so it is not double counted.
    assert inv.check(GroundTruth(orders=[order()]), 0).passed


def test_reserved_stock_must_equal_confirmed_quantity() -> None:
    inv = ReservedStockMatchesConfirmedOrders()
    assert inv.check(GOOD, 0).passed
    doubled = GroundTruth(
        orders=[order()], stock=[stock(6)], reservations=[reservation("r1"), reservation("r2")]
    )
    result = inv.check(doubled, 0)
    assert result.violations[0].detail == {
        "stock_reserved": 6,
        "active_reservation_quantity": 6,
        "confirmed_order_quantity": 3,
    }
    unconfirmed = GroundTruth(
        orders=[order(status="created")], stock=[stock(3)], reservations=[reservation()]
    )
    assert not inv.check(unconfirmed, 0).passed
    drifted_counter = GroundTruth(orders=[order()], stock=[stock(0)], reservations=[reservation()])
    assert not inv.check(drifted_counter, 0).passed
    released = GroundTruth(
        orders=[order(status="cancelled")],
        stock=[stock(0)],
        reservations=[reservation(status="released")],
    )
    assert inv.check(released, 0).passed


def test_invoice_without_reservation() -> None:
    inv = InvoiceHasReservation()
    assert inv.check(GOOD, 0).passed
    no_reservation = GroundTruth(orders=[order()], invoices=[invoice()])
    assert inv.check(no_reservation, 0).violations[0].entity_id == "i1"
    released = GroundTruth(invoices=[invoice()], reservations=[reservation(status="released")])
    assert not inv.check(released, 0).passed
    void = GroundTruth(invoices=[invoice(status="void")])
    assert inv.check(void, 0).passed


def test_confirmed_order_without_invoice() -> None:
    inv = ConfirmedOrderHasInvoice()
    assert inv.check(GOOD, 0).passed
    assert inv.check(GroundTruth(orders=[order()]), 0).violations[0].entity_id == "o1"
    only_void = GroundTruth(orders=[order()], invoices=[invoice(status="void")])
    assert not inv.check(only_void, 0).passed
    assert inv.check(GroundTruth(orders=[order(status="created")]), 0).passed


def test_orphaned_reservation_only_after_grace_period() -> None:
    inv = NoOrphanedReservations(grace_ms=1000)
    truth = GroundTruth(
        orders=[order(status="created")], reservations=[reservation(created_at_ms=500)]
    )
    assert inv.check(truth, now_ms=1500).passed  # exactly at the limit
    result = inv.check(truth, now_ms=1501)
    assert result.violations[0].detail == {
        "order_id": "o1",
        "order_status": "created",
        "age_ms": 1001,
    }
    no_order = GroundTruth(reservations=[reservation(order_id="ghost")])
    assert inv.check(no_order, now_ms=5000).violations[0].detail["order_status"] == "missing"
    assert inv.check(GOOD, now_ms=10**9).passed
    released = GroundTruth(reservations=[reservation(status="released")])
    assert inv.check(released, now_ms=10**9).passed


# --- against real service databases ---------------------------------------


async def test_happy_path_ground_truth_passes(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    spec = OrderWorkflow()
    await run_workflow(spec, client, DEFAULT_ORDER_INPUT)
    cluster.runtime.clock.advance(60_000)
    report = InvariantChecker(spec.invariants()).check(
        cluster.ground_truth(), cluster.runtime.clock.now_ms()
    )
    assert report.passed, report.violations


async def test_retried_invoice_is_caught_from_the_database(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    """What a naive retry after a lost response leaves behind."""
    spec = OrderWorkflow()
    ctx = await run_workflow(spec, client, DEFAULT_ORDER_INPUT)
    retry = await client.post(
        "http://billing/invoices", json={"order_id": ctx["order_id"], "amount_cents": 7500}
    )
    assert retry.status_code == 201  # the API is happy
    report = InvariantChecker(spec.invariants()).check(cluster.ground_truth(), 0)
    assert report.violation_counts() == {
        "single_invoice_per_confirmed_order": 1,
        "reserved_stock_matches_confirmed_orders": 0,
        "no_invoice_without_reservation": 0,
        "no_confirmed_order_without_invoice": 0,
        "no_orphaned_reservations": 0,
    }


async def test_abandoned_workflow_leaves_orphan_and_stock_mismatch(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    spec = OrderWorkflow(orphan_grace_ms=30_000)
    await run_workflow(spec, client, DEFAULT_ORDER_INPUT, stop_after="reserve_inventory")
    checker = InvariantChecker(spec.invariants())

    early = checker.check(cluster.ground_truth(), cluster.runtime.clock.now_ms())
    assert early.violation_counts()["no_orphaned_reservations"] == 0
    assert early.violation_counts()["reserved_stock_matches_confirmed_orders"] == 1

    cluster.runtime.clock.advance(30_001)
    late = checker.check(cluster.ground_truth(), cluster.runtime.clock.now_ms())
    assert late.violation_counts()["no_orphaned_reservations"] == 1


async def test_pending_async_work_is_reported_with_the_check(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    cluster.services["billing"].flaws.async_commit_delay_ms = 500
    await client.post("http://billing/invoices", json={"order_id": "o", "amount_cents": 1})
    report = InvariantChecker(order_invariants()).check(cluster.ground_truth(), 0)
    assert report.pending_async_operations == 1
