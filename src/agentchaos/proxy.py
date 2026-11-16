"""Fault injection proxy between the agent and the services.

Also records the three timeline lanes' raw material: what was delivered, what
the agent was told, and what actually changed in the service databases.
"""

from typing import Any

import httpx

from agentchaos.interfaces import FaultPolicy, ProxyRequest, ProxyResponse
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

DOMAIN_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (("orders", "id"),),
    "inventory": (("stock", "sku"), ("reservations", "id")),
    "billing": (("invoices", "id"),),
    "notifications": (("notifications", "id"),),
}


class Recorder:
    """Appends timeline events for one run, stamped with logical time."""

    def __init__(self, store: ResultsStore, run_id: str, cluster: ServiceCluster) -> None:
        self._store = store
        self._run_id = run_id
        self._clock = cluster.runtime.clock

    def emit(self, lane: str, kind: str, **payload: Any) -> None:
        self._store.append_event(
            self._run_id, lane=lane, kind=kind, logical_ms=self._clock.now_ms(), payload=payload
        )


class GroundTruthTracker:
    """Diffs the service databases and emits one event per real state change."""

    def __init__(self, cluster: ServiceCluster, recorder: Recorder) -> None:
        self._cluster = cluster
        self._recorder = recorder
        self._last = self._read()

    def _read(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        snapshot = self._cluster.snapshot()
        for service, tables in DOMAIN_TABLES.items():
            for table, key in tables:
                for row in snapshot[service][table]:
                    rows[(service, table, str(row[key]))] = row
        return rows

    def sync(self) -> int:
        current = self._read()
        changes = 0
        for ident in sorted(current):
            service, table, row_id = ident
            before, after = self._last.get(ident), current[ident]
            if before is None:
                self._recorder.emit(
                    "ground_truth",
                    "state_change",
                    service=service,
                    table=table,
                    id=row_id,
                    change="inserted",
                    row=after,
                )
                changes += 1
            elif before != after:
                fields = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
                self._recorder.emit(
                    "ground_truth",
                    "state_change",
                    service=service,
                    table=table,
                    id=row_id,
                    change="updated",
                    fields=fields,
                )
                changes += 1
        self._last = current
        return changes


class FaultProxy:
    def __init__(
        self,
        cluster: ServiceCluster,
        client: httpx.AsyncClient,
        recorder: Recorder,
        tracker: GroundTruthTracker,
        policy: FaultPolicy | None = None,
        *,
        latency_ms: int = 20,
        timeout_ms: int = 1000,
    ) -> None:
        self._cluster = cluster
        self._client = client
        self._recorder = recorder
        self._tracker = tracker
        self._policy = policy
        self._latency_ms = latency_ms
        self._timeout_ms = timeout_ms
        self.calls = 0
        self.injections = 0

    def advance(self, ms: int) -> None:
        """Move logical time, letting deferred commits fire, and record what changed."""
        self._cluster.runtime.scheduler.advance(ms)
        self._tracker.sync()

    async def _deliver(self, request: ProxyRequest) -> ProxyResponse:
        self.advance(self._latency_ms)
        req = request.request
        response = await self._client.request(
            req.method,
            f"http://{req.service}{req.path}",
            json=req.json,
            params=req.params,
            headers=dict(request.headers),
        )
        self._recorder.emit(
            "proxy",
            "delivered",
            step=request.step,
            purpose=request.purpose,
            service=req.service,
            method=req.method,
            path=req.path,
            status=response.status_code,
            idempotent_replay=response.headers.get("Idempotent-Replay") == "true",
        )
        self._tracker.sync()
        return ProxyResponse(response.status_code, response.json())

    async def send(self, request: ProxyRequest) -> ProxyResponse:
        self.calls += 1
        decision = self._policy.decide(request) if self._policy else None
        action = decision.action if decision else "pass"
        if decision and action != "pass":
            self.injections += 1
            self._recorder.emit(
                "proxy",
                "fault_injected",
                fault_type=decision.fault_type,
                step=request.step,
                attempt=request.attempt,
                explanation=decision.explanation,
            )

        if action == "fail_before_commit":
            self.advance(self._latency_ms)
            result = ProxyResponse(503, {"error": "service_unavailable"})
        else:
            actual = await self._deliver(request)
            if action == "duplicate_delivery":
                await self._deliver(request)
            if action == "timeout_after_commit":
                self.advance(self._timeout_ms)
                result = ProxyResponse(None, None, timed_out=True)
            else:
                result = actual

        self._recorder.emit(
            "proxy",
            "response_to_agent",
            step=request.step,
            purpose=request.purpose,
            attempt=request.attempt,
            status=result.status_code,
            timed_out=result.timed_out,
        )
        return result
