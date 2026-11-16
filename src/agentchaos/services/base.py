"""Shared machinery for the mock business services.

Each service owns one SQLite database. The flaws below are deliberate and
configurable, because they are what an agent trips over in real integrations.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Column,
    Connection,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    select,
)

from agentchaos.clock import ManualClock
from agentchaos.config import RESULTS_DB_FILENAME
from agentchaos.ids import SeededIdGenerator
from agentchaos.scheduler import DeterministicScheduler
from agentchaos.services.fixtures import DEFAULT_FIXTURE, Fixture

Snapshot = dict[str, list[dict[str, Any]]]
Commit = Callable[[Connection], tuple[int, dict[str, Any]]]


@dataclass
class ServiceFlaws:
    # False: the Idempotency-Key header is ignored (the common real-world case).
    idempotency_supported: bool = False
    # True: a second create for the same order is accepted as a new record.
    accept_duplicates: bool = True
    # > 0: mutations answer 202 immediately and commit this many logical ms later.
    async_commit_delay_ms: int = 0


@dataclass
class Runtime:
    """Deterministic primitives shared by every service in a cluster."""

    clock: ManualClock
    scheduler: DeterministicScheduler
    ids: SeededIdGenerator

    @classmethod
    def create(cls, seed: int) -> "Runtime":
        clock = ManualClock()
        return cls(clock, DeterministicScheduler(clock), SeededIdGenerator(seed))

    def reset(self, seed: int) -> None:
        self.clock.reset()
        self.scheduler.clear()
        self.ids.reseed(seed)


class Rejected(Exception):
    """Raised inside a commit function to roll back and answer with an error."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error", "rejected"))
        self.status = status
        self.body = body


class AdvanceClock(BaseModel):
    ms: int = Field(ge=0)


class FlawsUpdate(BaseModel):
    idempotency_supported: bool | None = None
    accept_duplicates: bool | None = None
    async_commit_delay_ms: int | None = Field(default=None, ge=0)


def request_hash(operation: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"op": operation, "payload": payload}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class MockService:
    name: ClassVar[str]

    def __init__(self, db_path: Path, runtime: Runtime, flaws: ServiceFlaws | None = None) -> None:
        if db_path.name == RESULTS_DB_FILENAME:
            raise ValueError("a mock service must never be pointed at the results database")
        self.db_path = db_path
        self.runtime = runtime
        self.flaws = flaws or ServiceFlaws()
        self.metadata = MetaData()
        self.idempotency_keys = Table(
            "idempotency_keys",
            self.metadata,
            Column("key", String, primary_key=True),
            Column("request_hash", String, nullable=False),
            Column("status_code", Integer, nullable=False),
            Column("body", JSON, nullable=False),
            Column("created_at_ms", Integer, nullable=False),
        )
        # Every mutation that reached the service, whether or not it changed state.
        self.request_log = Table(
            "request_log",
            self.metadata,
            Column("seq", Integer, primary_key=True, autoincrement=True),
            Column("operation", String, nullable=False),
            Column("idempotency_key", String, nullable=True),
            Column("request_hash", String, nullable=False),
            Column("phase", String, nullable=False),
            Column("status_code", Integer, nullable=False),
            Column("at_ms", Integer, nullable=False),
        )
        self.define_tables(self.metadata)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.metadata.create_all(self.engine)
        self.app = FastAPI(title=f"agentchaos mock: {self.name}")
        self._define_common_routes()
        self.define_routes(self.app)

    # --- subclass hooks -------------------------------------------------
    def define_tables(self, metadata: MetaData) -> None:
        raise NotImplementedError

    def define_routes(self, app: FastAPI) -> None:
        raise NotImplementedError

    def seed(self, conn: Connection, fixture: Fixture) -> None:
        """Insert fixture rows. Most services start empty."""

    # --- state ----------------------------------------------------------
    def reset(self, fixture: Fixture = DEFAULT_FIXTURE) -> None:
        self.metadata.drop_all(self.engine)
        self.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            self.seed(conn, fixture)

    def snapshot(self) -> Snapshot:
        out: Snapshot = {}
        with self.engine.connect() as conn:
            for table in self.metadata.sorted_tables:
                rows = conn.execute(select(table).order_by(*table.primary_key.columns))
                out[table.name] = [dict(row._mapping) for row in rows]
        return out

    def close(self) -> None:
        self.engine.dispose()

    # --- mutation pipeline ----------------------------------------------
    def _log(self, operation: str, key: str | None, rhash: str, phase: str, status: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.request_log).values(
                    operation=operation,
                    idempotency_key=key,
                    request_hash=rhash,
                    phase=phase,
                    status_code=status,
                    at_ms=self.runtime.clock.now_ms(),
                )
            )

    def _remember(self, conn: Connection, key: str, rhash: str, status: int, body: Any) -> None:
        conn.execute(
            insert(self.idempotency_keys).values(
                key=key,
                request_hash=rhash,
                status_code=status,
                body=body,
                created_at_ms=self.runtime.clock.now_ms(),
            )
        )

    def mutate(
        self,
        request: Request,
        operation: str,
        payload: dict[str, Any],
        commit: Commit,
        *,
        deferrable: bool = True,
        pending_body: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """Run one state-changing operation through idempotency and async handling."""
        key = request.headers.get("Idempotency-Key")
        rhash = request_hash(operation, payload)
        honour_key = self.flaws.idempotency_supported and key is not None

        if honour_key and key is not None:
            with self.engine.connect() as conn:
                seen = conn.execute(
                    select(self.idempotency_keys).where(self.idempotency_keys.c.key == key)
                ).first()
            if seen is not None:
                if seen.request_hash != rhash:
                    self._log(operation, key, rhash, "idempotency_conflict", 422)
                    return JSONResponse(
                        {"error": "idempotency_key_reused_with_different_request"}, 422
                    )
                self._log(operation, key, rhash, "replayed", seen.status_code)
                return JSONResponse(
                    seen.body, seen.status_code, headers={"Idempotent-Replay": "true"}
                )

        if deferrable and self.flaws.async_commit_delay_ms > 0:
            body = {"status": "processing", **(pending_body or {})}

            def deferred() -> None:
                try:
                    with self.engine.begin() as conn:
                        status, _ = commit(conn)
                    phase = "deferred_committed"
                except Rejected as rejected:
                    status, phase = rejected.status, "deferred_rejected"
                self._log(operation, key, rhash, phase, status)

            self.runtime.scheduler.schedule(
                self.flaws.async_commit_delay_ms, f"{self.name}:{operation}", deferred
            )
            if honour_key and key is not None:
                with self.engine.begin() as conn:
                    self._remember(conn, key, rhash, 202, body)
            self._log(operation, key, rhash, "accepted", 202)
            return JSONResponse(body, 202)

        try:
            with self.engine.begin() as conn:
                status, body = commit(conn)
                if honour_key and key is not None:
                    self._remember(conn, key, rhash, status, body)
            phase = "committed"
        except Rejected as rejected:
            status, body, phase = rejected.status, rejected.body, "rejected"
            if honour_key and key is not None:
                with self.engine.begin() as conn:
                    self._remember(conn, key, rhash, status, body)
        self._log(operation, key, rhash, phase, status)
        return JSONResponse(body, status)

    # --- routes every service has ---------------------------------------
    def _define_common_routes(self) -> None:
        app = self.app

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return {"service": self.name, "status": "ok", "now_ms": self.runtime.clock.now_ms()}

        @app.post("/_admin/reset")
        async def admin_reset() -> dict[str, Any]:
            self.reset(DEFAULT_FIXTURE)
            return {"service": self.name, "reset": True}

        @app.get("/_admin/clock")
        async def admin_clock() -> dict[str, Any]:
            pending = [
                {"due_ms": e.due_ms, "name": e.name} for e in self.runtime.scheduler.pending()
            ]
            return {"now_ms": self.runtime.clock.now_ms(), "pending": pending}

        @app.post("/_admin/clock/advance")
        async def admin_advance(body: AdvanceClock) -> dict[str, Any]:
            fired = self.runtime.scheduler.advance(body.ms)
            return {"now_ms": self.runtime.clock.now_ms(), "fired": fired}

        @app.get("/_admin/flaws")
        async def admin_get_flaws() -> dict[str, Any]:
            return asdict(self.flaws)

        @app.put("/_admin/flaws")
        async def admin_put_flaws(body: FlawsUpdate) -> dict[str, Any]:
            for name, value in body.model_dump(exclude_none=True).items():
                setattr(self.flaws, name, value)
            return asdict(self.flaws)
