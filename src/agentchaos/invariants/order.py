"""Invariants for the order workflow.

Only `issued` invoices and `active` reservations count; voided invoices and
released reservations are history, not current state.
"""

from collections import defaultdict
from dataclasses import dataclass

from agentchaos.invariants.base import GroundTruth, InvariantResult, Row, Violation


def _issued_invoices_by_order(truth: GroundTruth) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = defaultdict(list)
    for invoice in truth.invoices:
        if invoice["status"] == "issued":
            grouped[invoice["order_id"]].append(invoice)
    return grouped


def _active_reservations(truth: GroundTruth) -> list[Row]:
    return [r for r in truth.reservations if r["status"] == "active"]


def _confirmed_orders(truth: GroundTruth) -> list[Row]:
    return [o for o in truth.orders if o["status"] == "confirmed"]


@dataclass(frozen=True)
class SingleInvoicePerConfirmedOrder:
    """More than one issued invoice for a confirmed order.

    Together with `ConfirmedOrderHasInvoice` this is "exactly one invoice per
    confirmed order"; they are split so a missing invoice is counted once.
    """

    name: str = "single_invoice_per_confirmed_order"
    description: str = "A confirmed order has at most one issued invoice"

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult:
        invoices = _issued_invoices_by_order(truth)
        violations = tuple(
            Violation(
                self.name,
                "order",
                order["id"],
                {"issued_invoice_ids": sorted(i["id"] for i in invoices[order["id"]])},
            )
            for order in _confirmed_orders(truth)
            if len(invoices.get(order["id"], [])) > 1
        )
        return InvariantResult(self.name, violations)


@dataclass(frozen=True)
class ReservedStockMatchesConfirmedOrders:
    name: str = "reserved_stock_matches_confirmed_orders"
    description: str = "Per SKU, reserved stock equals the quantity of confirmed orders"

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult:
        confirmed: dict[str, int] = defaultdict(int)
        for order in _confirmed_orders(truth):
            confirmed[order["sku"]] += order["quantity"]
        active: dict[str, int] = defaultdict(int)
        for reservation in _active_reservations(truth):
            active[reservation["sku"]] += reservation["quantity"]
        reserved = {row["sku"]: row["reserved"] for row in truth.stock}
        violations = []
        for sku in sorted(set(confirmed) | set(active) | set(reserved)):
            counts = {
                "stock_reserved": reserved.get(sku, 0),
                "active_reservation_quantity": active.get(sku, 0),
                "confirmed_order_quantity": confirmed.get(sku, 0),
            }
            if len(set(counts.values())) > 1:
                violations.append(Violation(self.name, "sku", sku, counts))
        return InvariantResult(self.name, tuple(violations))


@dataclass(frozen=True)
class InvoiceHasReservation:
    name: str = "no_invoice_without_reservation"
    description: str = "Every issued invoice belongs to an order with an active reservation"

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult:
        reserved_orders = {r["order_id"] for r in _active_reservations(truth)}
        violations = tuple(
            Violation(self.name, "invoice", invoice["id"], {"order_id": invoice["order_id"]})
            for invoice in truth.invoices
            if invoice["status"] == "issued" and invoice["order_id"] not in reserved_orders
        )
        return InvariantResult(self.name, violations)


@dataclass(frozen=True)
class ConfirmedOrderHasInvoice:
    name: str = "no_confirmed_order_without_invoice"
    description: str = "Every confirmed order has an issued invoice"

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult:
        invoices = _issued_invoices_by_order(truth)
        violations = tuple(
            Violation(self.name, "order", order["id"], {})
            for order in _confirmed_orders(truth)
            if not invoices.get(order["id"])
        )
        return InvariantResult(self.name, violations)


@dataclass(frozen=True)
class NoOrphanedReservations:
    """An active reservation older than the grace period whose order is not confirmed."""

    grace_ms: int = 30_000
    name: str = "no_orphaned_reservations"
    description: str = "No active reservation outlives the grace period without a confirmed order"

    def check(self, truth: GroundTruth, now_ms: int) -> InvariantResult:
        status = {order["id"]: order["status"] for order in truth.orders}
        violations = tuple(
            Violation(
                self.name,
                "reservation",
                reservation["id"],
                {
                    "order_id": reservation["order_id"],
                    "order_status": status.get(reservation["order_id"], "missing"),
                    "age_ms": now_ms - reservation["created_at_ms"],
                },
            )
            for reservation in _active_reservations(truth)
            if status.get(reservation["order_id"]) != "confirmed"
            and now_ms - reservation["created_at_ms"] > self.grace_ms
        )
        return InvariantResult(self.name, violations)


def order_invariants(
    orphan_grace_ms: int = 30_000,
) -> tuple[
    SingleInvoicePerConfirmedOrder,
    ReservedStockMatchesConfirmedOrders,
    InvoiceHasReservation,
    ConfirmedOrderHasInvoice,
    NoOrphanedReservations,
]:
    return (
        SingleInvoicePerConfirmedOrder(),
        ReservedStockMatchesConfirmedOrders(),
        InvoiceHasReservation(),
        ConfirmedOrderHasInvoice(),
        NoOrphanedReservations(grace_ms=orphan_grace_ms),
    )
