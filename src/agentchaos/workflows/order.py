"""The v1 workflow: create order -> reserve inventory -> create invoice -> confirm -> notify.

Context keys: the input (`customer_id`, `sku`, `quantity`, `unit_price_cents`)
plus `order_id`, `reservation_id`, `invoice_id`, `notification_id` as steps complete.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentchaos.interfaces import Context, Invariant, StepRequest, WorkflowStep
from agentchaos.invariants.order import order_invariants


def _capture(key: str) -> Any:
    def capture(ctx: Context, body: Mapping[str, Any]) -> None:
        ctx[key] = body["id"]

    return capture


def _release(ctx: Context) -> StepRequest | None:
    if "reservation_id" not in ctx:
        return None
    return StepRequest("inventory", "POST", f"/reservations/{ctx['reservation_id']}/release")


def _void(ctx: Context) -> StepRequest | None:
    if "invoice_id" not in ctx:
        return None
    return StepRequest("billing", "POST", f"/invoices/{ctx['invoice_id']}/void")


def _cancel(ctx: Context) -> StepRequest | None:
    if "order_id" not in ctx:
        return None
    return StepRequest("orders", "POST", f"/orders/{ctx['order_id']}/cancel")


ORDER_STEPS: tuple[WorkflowStep, ...] = (
    WorkflowStep(
        name="create_order",
        service="orders",
        operation="create_order",
        build=lambda ctx: StepRequest(
            "orders",
            "POST",
            "/orders",
            json={
                "customer_id": ctx["customer_id"],
                "sku": ctx["sku"],
                "quantity": ctx["quantity"],
                "unit_price_cents": ctx["unit_price_cents"],
            },
        ),
        capture=_capture("order_id"),
        verify=lambda ctx: StepRequest(
            "orders", "GET", "/orders", params={"customer_id": ctx["customer_id"]}
        ),
        compensation=_cancel,
    ),
    WorkflowStep(
        name="reserve_inventory",
        service="inventory",
        operation="create_reservation",
        build=lambda ctx: StepRequest(
            "inventory",
            "POST",
            "/reservations",
            json={"order_id": ctx["order_id"], "sku": ctx["sku"], "quantity": ctx["quantity"]},
        ),
        capture=_capture("reservation_id"),
        verify=lambda ctx: StepRequest(
            "inventory", "GET", "/reservations", params={"order_id": ctx["order_id"]}
        ),
        compensation=_release,
    ),
    WorkflowStep(
        name="create_invoice",
        service="billing",
        operation="create_invoice",
        build=lambda ctx: StepRequest(
            "billing",
            "POST",
            "/invoices",
            json={
                "order_id": ctx["order_id"],
                "amount_cents": ctx["quantity"] * ctx["unit_price_cents"],
            },
        ),
        capture=_capture("invoice_id"),
        verify=lambda ctx: StepRequest(
            "billing", "GET", "/invoices", params={"order_id": ctx["order_id"]}
        ),
        compensation=_void,
    ),
    WorkflowStep(
        name="confirm_order",
        service="orders",
        operation="confirm_order",
        build=lambda ctx: StepRequest(
            "orders",
            "POST",
            f"/orders/{ctx['order_id']}/confirm",
            json={"reservation_id": ctx["reservation_id"], "invoice_id": ctx["invoice_id"]},
        ),
        capture=lambda ctx, body: None,
        verify=lambda ctx: StepRequest("orders", "GET", f"/orders/{ctx['order_id']}"),
        # A confirmed order cannot be un-confirmed; later failures need a human.
        compensation=None,
    ),
    WorkflowStep(
        name="notify_customer",
        service="notifications",
        operation="create_notification",
        build=lambda ctx: StepRequest(
            "notifications",
            "POST",
            "/notifications",
            json={
                "order_id": ctx["order_id"],
                "kind": "order_confirmed",
                "message": f"Order {ctx['order_id']} confirmed",
            },
        ),
        capture=_capture("notification_id"),
        verify=lambda ctx: StepRequest(
            "notifications", "GET", "/notifications", params={"order_id": ctx["order_id"]}
        ),
        compensation=None,
        optional=True,
    ),
)


@dataclass(frozen=True)
class OrderWorkflow:
    name: str = "order_fulfilment"
    steps: Sequence[WorkflowStep] = ORDER_STEPS
    orphan_grace_ms: int = 30_000

    def invariants(self) -> Sequence[Invariant]:
        return order_invariants(self.orphan_grace_ms)


DEFAULT_ORDER_INPUT: dict[str, Any] = {
    "customer_id": "cust_acme",
    "sku": "SKU-WIDGET",
    "quantity": 3,
    "unit_price_cents": 2500,
}
