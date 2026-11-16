"""Mock service behaviour, including each configurable flaw."""

from pathlib import Path

import httpx
import pytest

from agentchaos.interfaces import ServiceAdapter
from agentchaos.services.base import Runtime
from agentchaos.services.billing import BillingService
from agentchaos.services.cluster import ServiceCluster
from agentchaos.workflows.order import DEFAULT_ORDER_INPUT, OrderWorkflow
from tests.conftest import run_workflow

INVOICE = {"order_id": "ord_x", "amount_cents": 7500}
RESERVATION = {"order_id": "ord_x", "sku": "SKU-WIDGET", "quantity": 3}


async def test_happy_path_commits_one_of_everything(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    ctx = await run_workflow(OrderWorkflow(), client, DEFAULT_ORDER_INPUT)
    truth = cluster.ground_truth()
    assert [o["status"] for o in truth.orders] == ["confirmed"]
    assert truth.orders[0]["invoice_id"] == ctx["invoice_id"]
    assert [(r["order_id"], r["status"]) for r in truth.reservations] == [
        (ctx["order_id"], "active")
    ]
    assert [(i["order_id"], i["amount_cents"]) for i in truth.invoices] == [(ctx["order_id"], 7500)]
    assert len(truth.notifications) == 1
    widget = next(s for s in truth.stock if s["sku"] == "SKU-WIDGET")
    assert (widget["on_hand"], widget["reserved"]) == (100, 3)


async def test_services_satisfy_the_adapter_interface(cluster: ServiceCluster) -> None:
    for service in cluster.services.values():
        assert isinstance(service, ServiceAdapter)


async def test_validation_and_not_found(client: httpx.AsyncClient) -> None:
    bad = await client.post("http://orders/orders", json={"customer_id": "c", "sku": "s"})
    assert bad.status_code == 422
    assert (await client.get("http://orders/orders/nope")).status_code == 404
    assert (await client.post("http://billing/invoices/nope/void")).status_code == 404
    unknown_sku = await client.post(
        "http://inventory/reservations", json={**RESERVATION, "sku": "SKU-NOPE"}
    )
    assert unknown_sku.status_code == 404


async def test_insufficient_stock_is_rejected_and_changes_nothing(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "http://inventory/reservations", json={**RESERVATION, "sku": "SKU-SCARCE", "quantity": 2}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "insufficient_stock"
    truth = cluster.ground_truth()
    assert truth.reservations == []
    assert all(s["reserved"] == 0 for s in truth.stock)


# --- flaw: duplicates accepted ---------------------------------------------


async def test_flaw_duplicates_accepted_creates_two_invoices(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    first = await client.post("http://billing/invoices", json=INVOICE)
    second = await client.post("http://billing/invoices", json=INVOICE)
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
    assert len(cluster.ground_truth().invoices) == 2


async def test_duplicates_rejected_when_flaw_is_off(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    for service in cluster.services.values():
        service.flaws.accept_duplicates = False
    first = await client.post("http://billing/invoices", json=INVOICE)
    second = await client.post("http://billing/invoices", json=INVOICE)
    assert (first.status_code, second.status_code) == (201, 409)
    assert second.json() == {"error": "duplicate_invoice", "existing_id": first.json()["id"]}

    r1 = await client.post("http://inventory/reservations", json=RESERVATION)
    r2 = await client.post("http://inventory/reservations", json=RESERVATION)
    assert (r1.status_code, r2.status_code) == (201, 409)

    truth = cluster.ground_truth()
    assert len(truth.invoices) == 1
    assert len(truth.reservations) == 1


# --- flaw: no idempotency support ------------------------------------------


async def test_flaw_idempotency_key_is_ignored_when_unsupported(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    headers = {"Idempotency-Key": "k-1"}
    await client.post("http://billing/invoices", json=INVOICE, headers=headers)
    retry = await client.post("http://billing/invoices", json=INVOICE, headers=headers)
    assert retry.status_code == 201
    assert "Idempotent-Replay" not in retry.headers
    assert len(cluster.ground_truth().invoices) == 2


async def test_server_side_idempotency_replays_and_commits_once(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    cluster.services["billing"].flaws.idempotency_supported = True
    headers = {"Idempotency-Key": "k-1"}
    first = await client.post("http://billing/invoices", json=INVOICE, headers=headers)
    retry = await client.post("http://billing/invoices", json=INVOICE, headers=headers)
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replay"] == "true"
    assert retry.json() == first.json()
    assert len(cluster.ground_truth().invoices) == 1

    # Both requests reached the service; only one changed state.
    log = cluster.services["billing"].snapshot()["request_log"]
    assert [row["phase"] for row in log] == ["committed", "replayed"]

    mismatch = await client.post(
        "http://billing/invoices", json={**INVOICE, "amount_cents": 1}, headers=headers
    )
    assert mismatch.status_code == 422
    other_key = await client.post(
        "http://billing/invoices", json=INVOICE, headers={"Idempotency-Key": "k-2"}
    )
    assert other_key.status_code == 201
    assert len(cluster.ground_truth().invoices) == 2


# --- flaw: async processing ------------------------------------------------


async def test_flaw_async_returns_202_before_anything_is_committed(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    cluster.services["billing"].flaws.async_commit_delay_ms = 500
    response = await client.post("http://billing/invoices", json=INVOICE)
    assert response.status_code == 202
    assert response.json()["status"] == "processing"
    invoice_id = response.json()["id"]

    truth = cluster.ground_truth()
    assert truth.invoices == []
    assert truth.pending_async_operations == 1
    assert (await client.get(f"http://billing/invoices/{invoice_id}")).status_code == 404

    cluster.runtime.scheduler.advance(499)
    assert cluster.ground_truth().invoices == []
    cluster.runtime.scheduler.advance(1)
    truth = cluster.ground_truth()
    assert [(i["id"], i["created_at_ms"]) for i in truth.invoices] == [(invoice_id, 500)]
    assert truth.pending_async_operations == 0
    phases = [r["phase"] for r in cluster.services["billing"].snapshot()["request_log"]]
    assert phases == ["accepted", "deferred_committed"]


async def test_async_accepted_request_can_still_fail_to_commit(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    cluster.services["inventory"].flaws.async_commit_delay_ms = 100
    response = await client.post(
        "http://inventory/reservations", json={**RESERVATION, "sku": "SKU-SCARCE", "quantity": 5}
    )
    assert response.status_code == 202  # the API said yes
    cluster.runtime.scheduler.advance(100)
    assert cluster.ground_truth().reservations == []  # the database says no
    phases = [r["phase"] for r in cluster.services["inventory"].snapshot()["request_log"]]
    assert phases == ["accepted", "deferred_rejected"]


# --- compensating operations -----------------------------------------------


async def test_release_and_void_are_recorded_not_erased(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    reservation = (await client.post("http://inventory/reservations", json=RESERVATION)).json()
    invoice = (await client.post("http://billing/invoices", json=INVOICE)).json()
    cluster.runtime.clock.advance(40)
    for _ in range(2):  # both are safe to repeat
        released = await client.post(f"http://inventory/reservations/{reservation['id']}/release")
        voided = await client.post(f"http://billing/invoices/{invoice['id']}/void")
        assert (released.status_code, voided.status_code) == (200, 200)
    truth = cluster.ground_truth()
    assert [(r["status"], r["released_at_ms"]) for r in truth.reservations] == [("released", 40)]
    assert [(i["status"], i["voided_at_ms"]) for i in truth.invoices] == [("void", 40)]
    assert next(s for s in truth.stock if s["sku"] == "SKU-WIDGET")["reserved"] == 0


# --- admin + guards --------------------------------------------------------


async def test_admin_endpoints_drive_clock_flaws_and_reset(
    cluster: ServiceCluster, client: httpx.AsyncClient
) -> None:
    flaws = await client.put("http://billing/_admin/flaws", json={"async_commit_delay_ms": 50})
    assert flaws.json()["async_commit_delay_ms"] == 50
    await client.post("http://billing/invoices", json=INVOICE)
    advanced = await client.post("http://billing/_admin/clock/advance", json={"ms": 50})
    assert advanced.json() == {"now_ms": 50, "fired": ["billing:create_invoice"]}
    assert len(cluster.ground_truth().invoices) == 1
    await client.post("http://billing/_admin/reset")
    assert cluster.ground_truth().invoices == []
    assert (await client.get("http://billing/health")).json()["status"] == "ok"


def test_service_refuses_to_open_the_results_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="results database"):
        BillingService(tmp_path / "results.db", Runtime.create(1))
