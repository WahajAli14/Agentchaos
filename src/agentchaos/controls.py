"""The three safety controls. Each is middleware between the agent and the proxy.

What they do not promise:
- Idempotency keys only prevent duplicates if the SERVER honours them. A
  client-side dedupe store cannot help when the response was lost, because the
  client has nothing to remember.
- Compensation is a new business operation, not a rollback. The original write
  stays in history, and writes the agent never learned about are not compensated.
- Verify-after-write only runs when the response was ambiguous. A duplicate
  hidden behind a normal 2xx response is invisible to it.
"""

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, insert, select

from agentchaos.interfaces import (
    CallNext,
    ProxyRequest,
    ProxyResponse,
    WorkflowFailure,
    WorkflowSpec,
    WorkflowStep,
)
from agentchaos.proxy import Recorder

CONTROL_NAMES = ("idempotency", "idempotency_client_only", "saga", "verify_after_write")


class BaseControl:
    name = "base"

    async def around_call(self, request: ProxyRequest, call_next: CallNext) -> ProxyResponse:
        return await call_next(request)

    async def on_workflow_failed(self, failure: WorkflowFailure, send: CallNext) -> None:
        return None


class DedupeStore:
    """Durable client-side record of completed operations, keyed by idempotency key."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._metadata = MetaData()
        self._table = Table(
            "completed_operations",
            self._metadata,
            Column("key", String, primary_key=True),
            Column("status_code", Integer, nullable=False),
            Column("body", JSON, nullable=True),
        )
        self._engine = create_engine(f"sqlite:///{db_path}")
        self._metadata.create_all(self._engine)

    def reset(self) -> None:
        self._metadata.drop_all(self._engine)
        self._metadata.create_all(self._engine)

    def get(self, key: str) -> ProxyResponse | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(self._table).where(self._table.c.key == key)).first()
        if row is None:
            return None
        return ProxyResponse(row.status_code, row.body, source="dedupe_store")

    def put(self, key: str, response: ProxyResponse) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(self._table).values(
                    key=key, status_code=response.status_code, body=response.body
                )
            )

    def close(self) -> None:
        self._engine.dispose()


def idempotency_key(scope: str, step: str) -> str:
    """One key per logical operation: stable across retries, unique per run scope."""
    return "idem_" + hashlib.sha256(f"{scope}:{step}".encode()).hexdigest()[:20]


class IdempotencyControl(BaseControl):
    """Attaches a per-operation key and remembers completed operations.

    The same class backs both `idempotency` (services honour the key) and
    `idempotency_client_only` (services ignore it); the engine sets the service
    flag. The second exists to show the server-cooperation requirement.
    """

    def __init__(self, name: str, scope: str, store: DedupeStore, recorder: Recorder) -> None:
        self.name = name
        self._scope = scope
        self._store = store
        self._recorder = recorder

    async def around_call(self, request: ProxyRequest, call_next: CallNext) -> ProxyResponse:
        if request.purpose != "step" or request.request.method == "GET":
            return await call_next(request)
        key = idempotency_key(self._scope, request.step)
        remembered = self._store.get(key)
        if remembered is not None:
            self._recorder.emit("agent", "control_dedupe_hit", step=request.step, key=key)
            return remembered
        keyed = ProxyRequest(
            request.run_id,
            request.step,
            request.attempt,
            request.request,
            request.purpose,
            {**request.headers, "Idempotency-Key": key},
            request.context,
        )
        response = await call_next(keyed)
        if response.ok:
            self._store.put(key, response)
        return response


def _step(spec: WorkflowSpec, name: str) -> WorkflowStep:
    return next(step for step in spec.steps if step.name == name)


async def _find_committed(
    step: WorkflowStep, context: Any, run_id: str, send: CallNext
) -> tuple[bool, Any]:
    """Query actual state for a step's write. Returns (query worked, live record or None)."""
    if step.verify is None or step.find_committed is None:
        return False, None
    response = await send(
        ProxyRequest(run_id, step.name, 1, step.verify(dict(context)), purpose="verify")
    )
    if not response.ok:
        return False, None
    return True, step.find_committed(dict(context), response.body)


class VerifyAfterWriteControl(BaseControl):
    """After a timeout or 5xx, look at actual state before letting the agent retry."""

    name = "verify_after_write"

    def __init__(self, spec: WorkflowSpec, recorder: Recorder, run_id: str) -> None:
        self._spec = spec
        self._recorder = recorder
        self._run_id = run_id

    async def around_call(self, request: ProxyRequest, call_next: CallNext) -> ProxyResponse:
        response = await call_next(request)
        if request.purpose != "step" or not response.ambiguous:
            return response
        step = _step(self._spec, request.step)
        queried, found = await _find_committed(step, request.context, self._run_id, call_next)
        self._recorder.emit(
            "agent",
            "control_verify",
            step=request.step,
            queried=queried,
            committed=found is not None,
            record_id=found.get("id") if found else None,
        )
        if found is not None:
            return ProxyResponse(200, found, source="verified_state")
        return response

    async def on_workflow_failed(self, failure: WorkflowFailure, send: CallNext) -> None:
        """Escalate with a verified list of writes that are still live."""
        leftover: list[dict[str, Any]] = []
        unverifiable: list[str] = []
        for name in [*failure.completed_steps, failure.failed_step]:
            step = _step(self._spec, name)
            queried, found = await _find_committed(step, failure.context, self._run_id, send)
            if not queried:
                unverifiable.append(name)
            elif found is not None:
                leftover.append({"step": name, "id": found.get("id")})
        if leftover or unverifiable:
            self._recorder.emit(
                "agent",
                "escalation",
                verified=not unverifiable,
                source=self.name,
                failed_step=failure.failed_step,
                leftover=leftover,
                unverifiable=unverifiable,
                message="Workflow failed and these writes are still live; a person must decide.",
            )


class SagaControl(BaseControl):
    """On workflow failure, run compensations for completed steps in reverse order."""

    name = "saga"

    def __init__(
        self, spec: WorkflowSpec, recorder: Recorder, run_id: str, max_attempts: int = 3
    ) -> None:
        self._spec = spec
        self._recorder = recorder
        self._run_id = run_id
        self._max_attempts = max_attempts

    async def on_workflow_failed(self, failure: WorkflowFailure, send: CallNext) -> None:
        still_live: list[dict[str, Any]] = []
        for name in reversed(list(failure.completed_steps)):
            step = _step(self._spec, name)
            request = step.compensation(dict(failure.context)) if step.compensation else None
            if request is None:
                self._recorder.emit(
                    "agent", "control_compensation", step=name, status=None, skipped=True
                )
                still_live.append({"step": name, "reason": "no compensation defined"})
                continue
            status: int | None = None
            for attempt in range(1, self._max_attempts + 1):
                response = await send(
                    ProxyRequest(self._run_id, name, attempt, request, purpose="compensation")
                )
                status = response.status_code
                if response.ok:
                    break
            # Confirm from actual state that the compensation took effect.
            queried, found = await _find_committed(step, failure.context, self._run_id, send)
            undone = queried and found is None
            self._recorder.emit(
                "agent",
                "control_compensation",
                step=name,
                status=status,
                skipped=False,
                verified_undone=undone,
                note="Compensation is a new operation, not a rollback; the original write "
                "remains in history.",
            )
            if not undone:
                still_live.append({"step": name, "reason": f"compensation status {status}"})
        if still_live:
            self._recorder.emit(
                "agent",
                "escalation",
                verified=True,
                source=self.name,
                failed_step=failure.failed_step,
                leftover=still_live,
                unverifiable=[],
                message="Compensation did not undo every committed step; a person must decide.",
            )
