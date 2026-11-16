"""Fault policies and the proxy that applies them."""

import random

import httpx
import pytest

from agentchaos.faults import (
    ELIGIBLE_STEPS,
    FAULT_TYPES,
    DuplicateDelivery,
    PartialCommit,
    TimeoutAfterCommit,
    make_policy,
)
from agentchaos.interfaces import FaultPolicy, ProxyRequest, StepRequest
from agentchaos.proxy import FaultProxy, GroundTruthTracker, Recorder
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

INVOICE = StepRequest(
    "billing", "POST", "/invoices", json={"order_id": "ord_x", "amount_cents": 100}
)


def req(step: str = "create_invoice", attempt: int = 1, purpose: str = "step") -> ProxyRequest:
    return ProxyRequest("run_1", step, attempt, INVOICE, purpose=purpose)  # type: ignore[arg-type]


def test_timeout_and_duplicate_hit_only_the_first_attempt_of_the_target_step() -> None:
    for policy in (TimeoutAfterCommit("create_invoice"), DuplicateDelivery("create_invoice")):
        assert policy.decide(req()).action == policy.name
        assert policy.decide(req(attempt=2)).action == "pass"
        assert policy.decide(req(step="reserve_inventory")).action == "pass"
        assert policy.decide(req(purpose="verify")).action == "pass"
        assert policy.decide(req(purpose="compensation")).action == "pass"
        assert isinstance(policy, FaultPolicy)


def test_partial_commit_fails_every_attempt_of_the_target_step() -> None:
    policy = PartialCommit("create_invoice")
    assert [policy.decide(req(attempt=n)).action for n in (1, 2, 3)] == ["fail_before_commit"] * 3
    assert policy.decide(req(step="create_order")).action == "pass"
    assert policy.decide(req(purpose="compensation")).action == "pass"
    assert policy.decide(req()).fault_type == "partial_commit"


def test_make_policy_is_seeded_and_stays_within_eligible_steps() -> None:
    for fault_type in FAULT_TYPES:
        picks = [make_policy(fault_type, random.Random(s)).target_step for s in range(40)]
        assert picks == [make_policy(fault_type, random.Random(s)).target_step for s in range(40)]
        assert set(picks) == set(ELIGIBLE_STEPS[fault_type])
        assert make_policy(fault_type, random.Random(0)).name == fault_type
    assert make_policy("partial_commit", random.Random(0), "confirm_order").target_step == (
        "confirm_order"
    )
    with pytest.raises(ValueError):
        make_policy("meteor_strike", random.Random(0), "create_order")


def build(
    cluster: ServiceCluster, store: ResultsStore, client: httpx.AsyncClient, policy: FaultPolicy
) -> FaultProxy:
    store.create_run(
        "run_1",
        scenario="proxy",
        workflow="order_fulfilment",
        mode="deterministic",
        seed=1,
        fault_type=policy.name,
        controls=[],
        logical_ms=0,
    )
    recorder = Recorder(store, "run_1", cluster)
    return FaultProxy(cluster, client, recorder, GroundTruthTracker(cluster, recorder), policy)


def kinds(store: ResultsStore, lane: str) -> list[str]:
    return [e["kind"] for e in store.events("run_1") if e["lane"] == lane]


async def test_timeout_after_commit_commits_then_reports_timeout(
    cluster: ServiceCluster, store: ResultsStore, client: httpx.AsyncClient
) -> None:
    proxy = build(cluster, store, client, TimeoutAfterCommit("create_invoice"))
    response = await proxy.send(req())
    assert response.timed_out and response.status_code is None and response.ambiguous
    assert len(cluster.ground_truth().invoices) == 1  # it did happen
    assert cluster.runtime.clock.now_ms() == 20 + 1000
    assert kinds(store, "proxy") == ["fault_injected", "delivered", "response_to_agent"]
    assert kinds(store, "ground_truth") == ["state_change"]
    fault = next(e for e in store.events("run_1") if e["kind"] == "fault_injected")
    assert fault["payload"]["fault_type"] == "timeout_after_commit"
    assert "delivered and processed" in fault["payload"]["explanation"]

    retry = await proxy.send(req(attempt=2))
    assert retry.ok and retry.status_code == 201
    assert len(cluster.ground_truth().invoices) == 2
    assert proxy.calls == 2 and proxy.injections == 1


async def test_duplicate_delivery_delivers_twice_and_returns_one_response(
    cluster: ServiceCluster, store: ResultsStore, client: httpx.AsyncClient
) -> None:
    proxy = build(cluster, store, client, DuplicateDelivery("create_invoice"))
    response = await proxy.send(req())
    assert response.status_code == 201
    invoices = cluster.ground_truth().invoices
    assert len(invoices) == 2
    assert response.body["id"] == invoices[0]["id"]
    assert kinds(store, "proxy") == [
        "fault_injected",
        "delivered",
        "delivered",
        "response_to_agent",
    ]
    assert len(kinds(store, "ground_truth")) == 2


async def test_partial_commit_delivers_nothing(
    cluster: ServiceCluster, store: ResultsStore, client: httpx.AsyncClient
) -> None:
    proxy = build(cluster, store, client, PartialCommit("create_invoice"))
    response = await proxy.send(req())
    assert response.status_code == 503 and response.ambiguous and not response.ok
    assert cluster.ground_truth().invoices == []
    assert cluster.services["billing"].snapshot()["request_log"] == []
    assert kinds(store, "proxy") == ["fault_injected", "response_to_agent"]


async def test_tracker_records_updates_with_old_and_new_values(
    cluster: ServiceCluster, store: ResultsStore, client: httpx.AsyncClient
) -> None:
    proxy = build(cluster, store, client, PartialCommit("nothing"))
    created = await proxy.send(req())
    void = StepRequest("billing", "POST", f"/invoices/{created.body['id']}/void")
    await proxy.send(ProxyRequest("run_1", "create_invoice", 1, void, purpose="compensation"))
    changes = [e["payload"] for e in store.events("run_1") if e["kind"] == "state_change"]
    assert [c["change"] for c in changes] == ["inserted", "updated"]
    assert changes[1]["fields"]["status"] == ["issued", "void"]
