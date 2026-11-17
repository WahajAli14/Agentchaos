"""Run engine: one scenario, start to classified outcome.

    reset mock service DBs -> agent runs through controls and fault proxy
    -> bounded reconciliation if async work is outstanding
    -> invariants checked against the databases -> classification

Execution state and classification are separate. A run that is still unknown
after the bounded reconciliation attempts ends `indeterminate` and is never
classified.
"""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentchaos.agent import AgentResult, ScriptedAgent
from agentchaos.classifier import classify
from agentchaos.controls import (
    CONTROL_NAMES,
    BaseControl,
    DedupeStore,
    IdempotencyControl,
    SagaControl,
    VerifyAfterWriteControl,
)
from agentchaos.faults import make_policy
from agentchaos.interfaces import (
    CallNext,
    ProxyRequest,
    ProxyResponse,
    WorkflowFailure,
    WorkflowSpec,
)
from agentchaos.invariants.base import CheckReport, InvariantChecker
from agentchaos.llm import ProviderConfig, billed_cost_usd, estimated_cost_usd
from agentchaos.proxy import FaultProxy, GroundTruthTracker, Recorder
from agentchaos.results.states import Classification, ExecutionState
from agentchaos.results.store import ResultsStore, RunMetrics
from agentchaos.services.cluster import ServiceCluster
from agentchaos.tracing import get_tracer
from agentchaos.workflows.order import OrderWorkflow


class AgentLike(Protocol):
    async def run(self, workflow_input: dict[str, Any]) -> AgentResult: ...


AgentFactory = Callable[[WorkflowSpec, CallNext, Recorder, str], AgentLike]

SKUS = ("SKU-WIDGET", "SKU-GADGET")
ARRIVAL_PHASES = ("committed", "rejected", "replayed", "accepted", "noop", "idempotency_conflict")
COMMIT_PHASES = ("committed", "deferred_committed")


@dataclass(frozen=True)
class RunConfig:
    scenario: str
    seed: int
    fault_type: str | None = None
    controls: tuple[str, ...] = ()
    # Force the fault onto one step instead of letting the seed choose.
    target_step: str | None = None
    # service name -> async commit delay in logical ms (the async flaw).
    async_delay_ms: Mapping[str, int] = field(default_factory=dict)
    reconcile_attempts: int = 3
    reconcile_interval_ms: int = 500
    batch_id: str | None = None
    mode: str = "deterministic"  # deterministic | replay | live

    def __post_init__(self) -> None:
        unknown = set(self.controls) - set(CONTROL_NAMES)
        if unknown:
            raise ValueError(f"unknown controls: {sorted(unknown)}")
        if {"idempotency", "idempotency_client_only"} <= set(self.controls):
            raise ValueError("choose one of idempotency / idempotency_client_only")


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    state: ExecutionState
    classification: Classification | None
    explanation: str
    report: CheckReport
    target_step: str | None
    trace: list[dict[str, Any]] = field(default_factory=list)


def workflow_input(rng: random.Random) -> dict[str, Any]:
    return {
        "customer_id": "cust_acme",
        "sku": rng.choice(SKUS),
        "quantity": rng.randint(1, 4),
        "unit_price_cents": 2500,
    }


def count_duplicates(cluster: ServiceCluster) -> tuple[int, int]:
    """(duplicate requests that reached a service, duplicate operations that committed).

    Both come from the services' own request logs, grouped by request hash.
    """
    arrivals: dict[tuple[str, str], int] = {}
    commits: dict[tuple[str, str], int] = {}
    for name, service in cluster.services.items():
        for row in service.snapshot()["request_log"]:
            key = (name, row["request_hash"])
            if row["phase"] in ARRIVAL_PHASES:
                arrivals[key] = arrivals.get(key, 0) + 1
            if row["phase"] in COMMIT_PHASES:
                commits[key] = commits.get(key, 0) + 1
    return (
        sum(n - 1 for n in arrivals.values() if n > 1),
        sum(n - 1 for n in commits.values() if n > 1),
    )


class Engine:
    def __init__(
        self,
        cluster: ServiceCluster,
        store: ResultsStore,
        control_dir: Path,
        spec: WorkflowSpec | None = None,
    ) -> None:
        self.cluster = cluster
        self.store = store
        self.spec: WorkflowSpec = spec or OrderWorkflow()
        self._dedupe = DedupeStore(control_dir / "dedupe.db")
        self._orphan_grace_ms = getattr(self.spec, "orphan_grace_ms", 30_000)

    def close(self) -> None:
        self._dedupe.close()

    def _new_run_id(self) -> str:
        return f"run_{self.store.count_runs() + 1:06d}"

    def _build_controls(
        self, config: RunConfig, recorder: Recorder, run_id: str
    ) -> list[BaseControl]:
        scope = f"{self.spec.name}:{config.seed}"
        controls: list[BaseControl] = []
        # Order matters for failure handling: compensate first, then report what is left.
        if "saga" in config.controls:
            controls.append(SagaControl(self.spec, recorder, run_id))
        if "verify_after_write" in config.controls:
            controls.append(VerifyAfterWriteControl(self.spec, recorder, run_id))
        for name in ("idempotency", "idempotency_client_only"):
            if name in config.controls:
                controls.append(IdempotencyControl(name, scope, self._dedupe, recorder))
        return controls

    @staticmethod
    def _chain(controls: Sequence[BaseControl], proxy: FaultProxy) -> CallNext:
        send: CallNext = proxy.send
        for control in reversed(controls):

            def wrapped(
                request: ProxyRequest, control: BaseControl = control, inner: CallNext = send
            ) -> Any:
                return control.around_call(request, inner)

            send = wrapped
        return send

    async def run(
        self,
        config: RunConfig,
        agent_factory: "AgentFactory | None" = None,
        provider: ProviderConfig | None = None,
        order: dict[str, Any] | None = None,
    ) -> RunSummary:
        """Run one scenario. `agent_factory` swaps the scripted agent for a live
        LLM agent or a trace replay; `provider` is recorded for live runs."""
        with get_tracer().start_as_current_span("agentchaos.run") as span:
            span.set_attribute("agentchaos.scenario", config.scenario)
            span.set_attribute("agentchaos.seed", config.seed)
            span.set_attribute("agentchaos.mode", config.mode)
            span.set_attribute("agentchaos.fault_type", config.fault_type or "none")
            span.set_attribute("agentchaos.controls", ",".join(sorted(config.controls)) or "none")
            summary = await self._run(config, agent_factory, provider, order)
            span.set_attribute("agentchaos.run_id", summary.run_id)
            span.set_attribute("agentchaos.execution_state", summary.state.value)
            span.set_attribute(
                "agentchaos.classification",
                summary.classification.value if summary.classification else "none",
            )
            span.set_attribute("agentchaos.invariants_passed", summary.report.passed)
            return summary

    async def _run(
        self,
        config: RunConfig,
        agent_factory: "AgentFactory | None",
        provider: ProviderConfig | None,
        order: dict[str, Any] | None,
    ) -> RunSummary:
        cluster, store = self.cluster, self.store
        rng = random.Random(config.seed)
        seeded_order = workflow_input(rng)
        order = order or seeded_order
        policy = (
            make_policy(config.fault_type, rng, config.target_step) if config.fault_type else None
        )

        # Isolation: mock service DBs and the control store go back to the fixture.
        cluster.reset(config.seed)
        self._dedupe.reset()
        server_idempotency = "idempotency" in config.controls
        for name, service in cluster.services.items():
            service.flaws.idempotency_supported = server_idempotency
            service.flaws.accept_duplicates = True
            service.flaws.async_commit_delay_ms = config.async_delay_ms.get(name, 0)

        run_id = self._new_run_id()
        store.create_run(
            run_id,
            batch_id=config.batch_id,
            scenario=config.scenario,
            workflow=self.spec.name,
            mode=config.mode,
            provider=provider.name if provider else None,
            model=provider.model if provider else None,
            seed=config.seed,
            fault_type=config.fault_type,
            controls=list(config.controls),
            logical_ms=0,
        )
        recorder = Recorder(store, run_id, cluster)
        recorder.emit(
            "agent",
            "run_started",
            workflow_input=order,
            controls=sorted(config.controls),
            fault_type=config.fault_type,
            fault_target_step=policy.target_step if policy else None,
        )
        tracker = GroundTruthTracker(cluster, recorder)
        clock = cluster.runtime.clock

        async with cluster.client() as client:
            proxy = FaultProxy(cluster, client, recorder, tracker, policy)
            controls = self._build_controls(config, recorder, run_id)
            send = self._chain(controls, proxy)
            agent: AgentLike = (
                agent_factory(self.spec, send, recorder, run_id)
                if agent_factory
                else ScriptedAgent(self.spec, send, recorder, run_id)
            )
            result = await agent.run(order)
            if result.failed_step is not None:
                failure = WorkflowFailure(
                    dict(result.context), tuple(result.completed_steps), result.failed_step
                )

                async def control_send(request: ProxyRequest) -> ProxyResponse:
                    return await proxy.send(request)

                for control in controls:
                    await control.on_workflow_failed(failure, control_send)
            tool_calls = proxy.calls

        agent_done_ms = clock.now_ms()

        # Bounded reconciliation: wait for accepted-but-uncommitted writes.
        pending = len(cluster.runtime.scheduler.pending())
        if pending:
            store.transition(
                run_id,
                ExecutionState.PENDING_RECONCILIATION,
                clock.now_ms(),
                f"{pending} accepted write(s) not yet committed",
            )
            for attempt in range(1, config.reconcile_attempts + 1):
                proxy.advance(config.reconcile_interval_ms)
                pending = len(cluster.runtime.scheduler.pending())
                store.record_reconciliation_attempt(
                    run_id,
                    attempt=attempt,
                    logical_ms=clock.now_ms(),
                    pending_operations=pending,
                    result="settled" if pending == 0 else "still_pending",
                )
                if pending == 0:
                    break

        checker = InvariantChecker(self.spec.invariants())
        duplicate_requests, duplicate_commits = count_duplicates(cluster)

        tokens = (result.prompt_tokens, result.completion_tokens)

        def metrics() -> RunMetrics:
            return RunMetrics(
                tool_calls=tool_calls,
                duplicate_requests=duplicate_requests,
                duplicate_committed_operations=duplicate_commits,
                llm_calls=result.llm_calls,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                # No LLM call means nothing billed and nothing to estimate.
                billed_cost_usd=billed_cost_usd(provider, *tokens) if provider else 0.0,
                estimated_cost_usd=estimated_cost_usd(provider, *tokens) if provider else 0.0,
                # Live: measured wall time of the agent loop. Otherwise logical
                # milliseconds until the agent and controls finished.
                latency_ms=result.wall_latency_ms
                if result.wall_latency_ms is not None
                else float(agent_done_ms),
            )

        if pending:
            report = checker.check(cluster.ground_truth(), clock.now_ms())
            store.record_invariant_report(run_id, report)
            explanation = (
                f"{pending} accepted write(s) still uncommitted after "
                f"{config.reconcile_attempts} reconciliation attempts; outcome unknown, escalated."
            )
            store.transition(run_id, ExecutionState.INDETERMINATE, clock.now_ms(), explanation)
            store.record_outcome(
                run_id,
                classification=None,
                explanation=explanation,
                agent_believed_outcome=result.believed_outcome,
                goal_achieved=None,
                metrics=metrics(),
            )
            return RunSummary(
                run_id,
                ExecutionState.INDETERMINATE,
                None,
                explanation,
                report,
                policy.target_step if policy else None,
                result.trace,
            )

        # Let the orphan grace period elapse so "abandoned" is decidable.
        proxy.advance(self._orphan_grace_ms + 1)
        truth = cluster.ground_truth()
        report = checker.check(truth, clock.now_ms())
        store.record_invariant_report(run_id, report)
        recorder.emit(
            "ground_truth",
            "invariants_checked",
            passed=report.passed,
            violation_counts=report.violation_counts(),
        )
        goal_achieved = any(
            o["id"] == result.context.get("order_id") and o["status"] == "confirmed"
            for o in truth.orders
        )
        verified_escalation = any(
            e["kind"] == "escalation" and e["payload"].get("verified") for e in store.events(run_id)
        )
        classification, explanation = classify(
            report,
            goal_achieved=goal_achieved,
            duplicate_committed_operations=duplicate_commits,
            verified_escalation=verified_escalation,
        )
        store.transition(run_id, ExecutionState.RESOLVED, clock.now_ms(), "final state verified")
        store.record_outcome(
            run_id,
            classification=classification,
            explanation=explanation,
            agent_believed_outcome=result.believed_outcome,
            goal_achieved=goal_achieved,
            metrics=metrics(),
        )
        return RunSummary(
            run_id,
            ExecutionState.RESOLVED,
            classification,
            explanation,
            report,
            policy.target_step if policy else None,
            result.trace,
        )


def canonical_record(store: ResultsStore, run_id: str) -> dict[str, Any]:
    """The part of a run that must be identical for the same seed and config.

    Excludes run id, database surrogate ids and wall-clock `recorded_at`.
    """
    outcome = store.outcome(run_id) or {}
    return {
        "events": [
            (e["seq"], e["lane"], e["kind"], e["logical_ms"], e["payload"])
            for e in store.events(run_id)
        ],
        "states": [state.value for state in store.state_history(run_id)],
        "reconciliation": [
            (a["attempt"], a["logical_ms"], a["pending_operations"], a["result"])
            for a in store.reconciliation_log(run_id)
        ],
        "invariants": [
            (r["invariant"], r["passed"], r["violations"]) for r in store.invariant_results(run_id)
        ],
        "outcome": {
            key: value for key, value in outcome.items() if key not in ("run_id", "recorded_at")
        },
    }
