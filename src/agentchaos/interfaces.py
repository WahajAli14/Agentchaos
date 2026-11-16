"""Extension points. New services, workflows, invariants, faults and controls
implement these protocols; the engine depends only on them.

"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import FastAPI

from agentchaos.invariants.base import Invariant

__all__ = [
    "FaultPolicy",
    "Invariant",
    "SafetyControl",
    "ServiceAdapter",
    "WorkflowSpec",
    "WorkflowStep",
]

Snapshot = dict[str, list[dict[str, Any]]]


@runtime_checkable
class ServiceAdapter(Protocol):
    """A business service the agent can call, plus direct access to its real state."""

    name: str
    app: FastAPI

    def reset(self, fixture: Any) -> None:
        """Restore the service database to a known fixture."""

    def snapshot(self) -> Snapshot:
        """Read every table straight from the database (ground truth, not the API)."""


@dataclass(frozen=True)
class StepRequest:
    service: str
    method: str
    path: str
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


Context = dict[str, Any]


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    service: str
    operation: str
    build: Callable[[Context], StepRequest]
    # Copies identifiers from a successful response body into the workflow context.
    capture: Callable[[Context, Mapping[str, Any]], None]
    # Read-only query that shows whether this step's write actually committed,
    # and a function that picks the live committed record out of its response.
    verify: Callable[[Context], StepRequest] | None = None
    find_committed: Callable[[Context, Any], Mapping[str, Any] | None] | None = None
    # Undo request for a committed step. Compensation is a new business
    # operation, not a rollback: the original write stays in history.
    compensation: Callable[[Context], StepRequest | None] | None = None
    optional: bool = False


@runtime_checkable
class WorkflowSpec(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def steps(self) -> Sequence[WorkflowStep]: ...

    def invariants(self) -> Sequence[Invariant]: ...


Purpose = Literal["step", "verify", "compensation"]


@dataclass(frozen=True)
class ProxyRequest:
    run_id: str
    step: str
    attempt: int
    request: StepRequest
    purpose: Purpose = "step"
    headers: Mapping[str, str] = field(default_factory=dict)
    # Workflow context at the time of the call (read-only for controls).
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProxyResponse:
    status_code: int | None  # None when the caller observed a timeout
    body: Any = None
    timed_out: bool = False
    # service | dedupe_store | verified_state
    source: str = "service"

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.status_code is not None and self.status_code < 300

    @property
    def ambiguous(self) -> bool:
        """The caller cannot tell whether the write happened."""
        return self.timed_out or (self.status_code is not None and self.status_code >= 500)


FaultAction = Literal["pass", "timeout_after_commit", "duplicate_delivery", "fail_before_commit"]


@dataclass(frozen=True)
class FaultDecision:
    action: FaultAction = "pass"
    fault_type: str | None = None
    explanation: str = ""


@runtime_checkable
class FaultPolicy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def target_step(self) -> str: ...

    def decide(self, request: ProxyRequest) -> FaultDecision: ...


CallNext = Callable[[ProxyRequest], Awaitable[ProxyResponse]]


@dataclass(frozen=True)
class WorkflowFailure:
    """What the agent knows when it gives up on a workflow."""

    context: Mapping[str, Any]
    completed_steps: Sequence[str]
    failed_step: str


@runtime_checkable
class SafetyControl(Protocol):
    """Middleware between the agent and the fault proxy."""

    @property
    def name(self) -> str: ...

    async def around_call(self, request: ProxyRequest, call_next: CallNext) -> ProxyResponse: ...

    async def on_workflow_failed(self, failure: WorkflowFailure, send: CallNext) -> None: ...
