"""Append-only results database.

Rows are only ever inserted. SQLite triggers reject UPDATE and DELETE on every
table, so historical benchmark records cannot be rewritten by application code.
A run's current execution state is its latest transition row.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    func,
    insert,
    select,
    text,
)

from agentchaos.invariants.base import CheckReport
from agentchaos.results.states import (
    ALLOWED_TRANSITIONS,
    Classification,
    ExecutionState,
    InvalidOutcome,
    InvalidTransition,
)

metadata = MetaData()

# `recorded_at` is filled by SQLite. It is wall-clock on purpose (when the row
# was stored) and is excluded from canonical run records.
runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("batch_id", String, nullable=True, index=True),
    Column("scenario", String, nullable=False),
    Column("workflow", String, nullable=False),
    Column("mode", String, nullable=False),  # deterministic | live
    Column("seed", Integer, nullable=False),
    Column("fault_type", String, nullable=True),
    Column("controls", JSON, nullable=False),
    Column("provider", String, nullable=True),
    Column("model", String, nullable=True),
    Column("recorded_at", DateTime, server_default=func.current_timestamp(), nullable=False),
)

run_state_transitions = Table(
    "run_state_transitions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("logical_ms", Integer, nullable=False),
    Column("reason", String, nullable=False),
    Column("recorded_at", DateTime, server_default=func.current_timestamp(), nullable=False),
    UniqueConstraint("run_id", "seq"),
)

run_events = Table(
    "run_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("lane", String, nullable=False),  # agent | proxy | ground_truth
    Column("kind", String, nullable=False),
    Column("logical_ms", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    UniqueConstraint("run_id", "seq"),
)

reconciliation_attempts = Table(
    "reconciliation_attempts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("attempt", Integer, nullable=False),
    Column("logical_ms", Integer, nullable=False),
    Column("pending_operations", Integer, nullable=False),
    Column("result", String, nullable=False),
    UniqueConstraint("run_id", "attempt"),
)

invariant_results = Table(
    "invariant_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String, nullable=False, index=True),
    Column("invariant", String, nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("violations", JSON, nullable=False),
    Column("checked_at_ms", Integer, nullable=False),
    UniqueConstraint("run_id", "invariant"),
)

run_outcomes = Table(
    "run_outcomes",
    metadata,
    Column("run_id", String, primary_key=True),
    # NULL classification is only valid for indeterminate runs.
    Column("classification", String, nullable=True),
    Column("final_state", String, nullable=False),
    Column("explanation", String, nullable=False),
    # What the agent reported, as opposed to what the databases show.
    Column("agent_believed_outcome", String, nullable=True),
    Column("goal_achieved", Boolean, nullable=True),
    Column("tool_calls", Integer, nullable=False),
    Column("llm_calls", Integer, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("duplicate_requests", Integer, nullable=False),
    Column("duplicate_committed_operations", Integer, nullable=False),
    Column("billed_cost_usd", Float, nullable=False),
    # NULL means unknown (no public list price). Never guessed.
    Column("estimated_cost_usd", Float, nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("recorded_at", DateTime, server_default=func.current_timestamp(), nullable=False),
)


@dataclass(frozen=True)
class RunMetrics:
    tool_calls: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duplicate_requests: int = 0
    duplicate_committed_operations: int = 0
    billed_cost_usd: float = 0.0
    estimated_cost_usd: float | None = None
    latency_ms: float | None = None


class ResultsStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.engine = create_engine(f"sqlite:///{db_path}")
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            for table in metadata.sorted_tables:
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS {table.name}_no_{action.lower()} "
                            f"BEFORE {action} ON {table.name} BEGIN "
                            f"SELECT RAISE(ABORT, 'results are append-only: "
                            f"{action} on {table.name} is not allowed'); END"
                        )
                    )

    def close(self) -> None:
        self.engine.dispose()

    # --- writes (inserts only) ------------------------------------------
    def create_run(
        self,
        run_id: str,
        *,
        scenario: str,
        workflow: str,
        mode: str,
        seed: int,
        fault_type: str | None,
        controls: list[str],
        logical_ms: int,
        batch_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(runs).values(
                    run_id=run_id,
                    batch_id=batch_id,
                    scenario=scenario,
                    workflow=workflow,
                    mode=mode,
                    seed=seed,
                    fault_type=fault_type,
                    controls=sorted(controls),
                    provider=provider,
                    model=model,
                )
            )
        self.transition(run_id, ExecutionState.RUNNING, logical_ms, "run started")

    def transition(self, run_id: str, state: ExecutionState, logical_ms: int, reason: str) -> None:
        history = self.state_history(run_id)
        current = history[-1] if history else None
        if state not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransition(f"{run_id}: {current} -> {state} is not allowed")
        with self.engine.begin() as conn:
            conn.execute(
                insert(run_state_transitions).values(
                    run_id=run_id,
                    seq=len(history),
                    state=state.value,
                    logical_ms=logical_ms,
                    reason=reason,
                )
            )

    def append_event(
        self, run_id: str, *, lane: str, kind: str, logical_ms: int, payload: dict[str, Any]
    ) -> int:
        with self.engine.begin() as conn:
            seq = conn.execute(
                select(func.count()).select_from(run_events).where(run_events.c.run_id == run_id)
            ).scalar_one()
            conn.execute(
                insert(run_events).values(
                    run_id=run_id,
                    seq=seq,
                    lane=lane,
                    kind=kind,
                    logical_ms=logical_ms,
                    payload=payload,
                )
            )
        return int(seq)

    def record_reconciliation_attempt(
        self, run_id: str, *, attempt: int, logical_ms: int, pending_operations: int, result: str
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(reconciliation_attempts).values(
                    run_id=run_id,
                    attempt=attempt,
                    logical_ms=logical_ms,
                    pending_operations=pending_operations,
                    result=result,
                )
            )

    def record_invariant_report(self, run_id: str, report: CheckReport) -> None:
        with self.engine.begin() as conn:
            for result in report.results:
                conn.execute(
                    insert(invariant_results).values(
                        run_id=run_id,
                        invariant=result.invariant,
                        passed=result.passed,
                        violations=[asdict(v) for v in result.violations],
                        checked_at_ms=report.checked_at_ms,
                    )
                )

    def record_outcome(
        self,
        run_id: str,
        *,
        classification: Classification | None,
        explanation: str,
        agent_believed_outcome: str | None = None,
        goal_achieved: bool | None = None,
        metrics: RunMetrics | None = None,
    ) -> None:
        """Store the final outcome. Refuses any outcome the stored evidence does not support."""
        state = self.current_state(run_id)
        if state is ExecutionState.INDETERMINATE:
            if classification is not None:
                raise InvalidOutcome("an indeterminate run cannot be classified")
        elif state is ExecutionState.RESOLVED:
            if classification is None:
                raise InvalidOutcome("a resolved run needs a classification")
            checks = self.invariant_results(run_id)
            if not checks:
                raise InvalidOutcome("cannot classify a run with no stored invariant results")
            if classification is Classification.VERIFIED_RECOVERY and not all(
                c["passed"] for c in checks
            ):
                raise InvalidOutcome("verified_recovery is not allowed with invariant violations")
        else:
            raise InvalidOutcome(
                f"run is {state}; only resolved or indeterminate runs get outcomes"
            )
        with self.engine.begin() as conn:
            conn.execute(
                insert(run_outcomes).values(
                    run_id=run_id,
                    classification=classification.value if classification else None,
                    final_state=state.value,
                    explanation=explanation,
                    agent_believed_outcome=agent_believed_outcome,
                    goal_achieved=goal_achieved,
                    **asdict(metrics or RunMetrics()),
                )
            )

    # --- reads ----------------------------------------------------------
    def _rows(self, table: Table, run_id: str, order: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            query = select(table).where(table.c.run_id == run_id).order_by(table.c[order])
            return [dict(row._mapping) for row in conn.execute(query)]

    def state_history(self, run_id: str) -> list[ExecutionState]:
        return [
            ExecutionState(row["state"]) for row in self._rows(run_state_transitions, run_id, "seq")
        ]

    def current_state(self, run_id: str) -> ExecutionState | None:
        history = self.state_history(run_id)
        return history[-1] if history else None

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows(run_events, run_id, "seq")

    def reconciliation_log(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows(reconciliation_attempts, run_id, "attempt")

    def invariant_results(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows(invariant_results, run_id, "id")

    def outcome(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows(run_outcomes, run_id, "run_id")
        return rows[0] if rows else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows(runs, run_id, "run_id")
        return rows[0] if rows else None

    def count_runs(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(runs)).scalar_one())

    def list_runs(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        """Runs joined with their outcome (outcome columns are None while unfinished)."""
        query = (
            select(runs, *[c for c in run_outcomes.c if c.name not in ("run_id", "recorded_at")])
            .select_from(runs.outerjoin(run_outcomes, runs.c.run_id == run_outcomes.c.run_id))
            .order_by(runs.c.recorded_at, runs.c.run_id)
        )
        if batch_id is not None:
            query = query.where(runs.c.batch_id == batch_id)
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(query)]

    def all_invariant_results(self, batch_id: str) -> list[dict[str, Any]]:
        query = (
            select(invariant_results)
            .join(runs, runs.c.run_id == invariant_results.c.run_id)
            .where(runs.c.batch_id == batch_id)
        )
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(query)]

    def latest_batch_id(self) -> str | None:
        query = (
            select(runs.c.batch_id)
            .where(runs.c.batch_id.is_not(None))
            .order_by(runs.c.recorded_at.desc(), runs.c.run_id.desc())
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
        return row[0] if row else None
