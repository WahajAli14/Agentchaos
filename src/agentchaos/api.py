"""HTTP API over the results database, and the web UI.

    uvicorn agentchaos.api:app --factory

Reads come straight from the append-only results store. Two endpoints execute
deterministic runs (one scenario, or the whole benchmark matrix); they share
one engine and run one at a time. If the built frontend exists it is served
from `/`; otherwise two plain HTML pages are.
"""

import asyncio
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agentchaos.bench import ALL_CONTROLS, run_matrix
from agentchaos.config import get_settings, log_provider_statuses
from agentchaos.controls import CONTROL_NAMES
from agentchaos.engine import Engine, RunConfig
from agentchaos.faults import ELIGIBLE_STEPS, FAULT_TYPES
from agentchaos.invariants.order import order_invariants
from agentchaos.llm_pool import ProviderPool
from agentchaos.metrics import (
    benchmark_matrix,
    controls_delta,
    controls_label,
    invariant_breakdown,
    live_model_stats,
)
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

STATIC = Path(__file__).parent / "static"
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

FAULT_DESCRIPTIONS = {
    "timeout_after_commit": "The request reaches the service and commits. The response is lost, "
    "so the agent sees a timeout.",
    "duplicate_delivery": "The request is delivered to the service twice. The agent sees one "
    "normal response.",
    "partial_commit": "Earlier steps commit, then the next step's service is unavailable for "
    "every attempt.",
}
CONTROL_DESCRIPTIONS = {
    "idempotency": "Per-operation idempotency key, honoured by the services.",
    "idempotency_client_only": "Idempotency key sent, but the services ignore it. Shows that "
    "keys need server cooperation.",
    "saga": "On workflow failure, run compensations in reverse order. Compensation is a new "
    "operation, not a rollback.",
    "verify_after_write": "After a timeout or 5xx, query actual state before retrying.",
}


class RunRequest(BaseModel):
    fault_type: str | None = None
    controls: list[str] = Field(default_factory=list)
    seed: int = 42
    target_step: str | None = None


class BenchmarkRequest(BaseModel):
    seeds: int = Field(default=20, ge=1, le=200)
    start_seed: int = 42


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def mismatches(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Places where what the agent was told differs from what actually happened."""
    return [
        {
            "seq": event["seq"],
            "logical_ms": event["logical_ms"],
            "step": event["payload"]["step"],
            "fault_type": event["payload"]["fault_type"],
            "explanation": event["payload"]["explanation"],
        }
        for event in events
        if event["kind"] == "fault_injected"
    ]


def create_app(
    store: ResultsStore,
    engine: Engine | None = None,
    web_dist: Path | None = None,
    pool: ProviderPool | None = None,
) -> FastAPI:
    api = FastAPI(title="agentchaos")
    lock = asyncio.Lock()

    def need_engine() -> Engine:
        if engine is None:
            raise HTTPException(503, "this server is read-only: no run engine configured")
        return engine

    @api.get("/api/scenarios")
    async def scenarios() -> dict[str, Any]:
        return {
            "default_seed": get_settings().default_seed,
            "can_run": engine is not None,
            "faults": [
                {
                    "fault_type": name,
                    "description": FAULT_DESCRIPTIONS[name],
                    "eligible_steps": list(ELIGIBLE_STEPS[name]),
                }
                for name in FAULT_TYPES
            ],
            "controls": [
                {"name": name, "description": CONTROL_DESCRIPTIONS[name]} for name in CONTROL_NAMES
            ],
        }

    @api.get("/api/runs")
    async def list_runs(batch_id: str | None = None) -> list[dict[str, Any]]:
        return [_json_safe(run) for run in store.list_runs(batch_id)]

    @api.post("/api/runs", status_code=201)
    async def start_run(body: RunRequest) -> dict[str, Any]:
        runner = need_engine()
        try:
            config = RunConfig(
                scenario=f"{body.fault_type or 'no_fault'}/{controls_label(body.controls)}",
                seed=body.seed,
                fault_type=body.fault_type,
                controls=tuple(body.controls),
                target_step=body.target_step,
                batch_id="adhoc",
            )
            if body.fault_type is not None and body.fault_type not in FAULT_TYPES:
                raise ValueError(f"unknown fault type: {body.fault_type}")
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        async with lock:
            summary = await runner.run(config)
        return {
            "run_id": summary.run_id,
            "state": summary.state.value,
            "classification": summary.classification.value if summary.classification else None,
        }

    @api.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        events = store.events(run_id)
        outcome = store.outcome(run_id)
        return {
            "run": _json_safe(run),
            "states": [state.value for state in store.state_history(run_id)],
            "events": [
                {k: e[k] for k in ("seq", "lane", "kind", "logical_ms", "payload")} for e in events
            ],
            "mismatches": mismatches(events),
            "reconciliation": [_json_safe(a) for a in store.reconciliation_log(run_id)],
            "invariants": [
                {k: r[k] for k in ("invariant", "passed", "violations")}
                for r in store.invariant_results(run_id)
            ],
            "outcome": _json_safe(outcome) if outcome else None,
        }

    @api.get("/api/batches")
    async def batches() -> list[dict[str, Any]]:
        return [_json_safe(batch) for batch in store.list_batches()]

    @api.get("/api/benchmark")
    async def benchmark(batch_id: str | None = None) -> dict[str, Any]:
        batch = batch_id or latest_matrix_batch()
        if batch is None:
            return {"batch_id": None, "cells": [], "deltas": []}
        cells = benchmark_matrix(store, batch)
        all_on = controls_label(ALL_CONTROLS)
        deltas = [controls_delta(cells, fault, all_on) for fault in FAULT_TYPES]
        return {
            "batch_id": batch,
            "cells": [cell.as_dict() for cell in cells],
            "deltas": [d for d in deltas if d is not None],
        }

    @api.post("/api/benchmark", status_code=201)
    async def start_benchmark(body: BenchmarkRequest) -> dict[str, Any]:
        runner = need_engine()
        async with lock:
            batch_id = await run_matrix(runner, body.seeds, body.start_seed)
        return {"batch_id": batch_id}

    def latest_matrix_batch() -> str | None:
        return next(
            (b["batch_id"] for b in store.list_batches() if b["batch_id"].startswith("batch_")),
            None,
        )

    @api.get("/api/invariants")
    async def invariants(batch_id: str | None = None) -> dict[str, Any]:
        descriptions = {inv.name: inv.description for inv in order_invariants()}
        batch = batch_id or latest_matrix_batch()
        if batch is None:
            return {"batch_id": None, "invariants": []}
        return {
            "batch_id": batch,
            "invariants": invariant_breakdown(store, batch, descriptions),
        }

    @api.get("/api/providers")
    async def providers() -> dict[str, Any]:
        """Provider state. Keys are only ever present masked."""
        return {
            "providers": pool.status() if pool else [],
            "failovers": pool.failover_log() if pool else [],
            "live_models": live_model_stats(store),
        }

    @api.post("/api/providers/check")
    async def check_providers() -> dict[str, Any]:
        if pool is None:
            raise HTTPException(503, "no provider pool configured")
        async with lock:
            await pool.health_check()
        return {"providers": pool.status()}

    @api.get("/api/settings")
    async def settings_view() -> dict[str, Any]:
        """Non-secret settings only."""
        current = get_settings()
        return {
            "env": current.env,
            "default_seed": current.default_seed,
            "live_mode_enabled": current.live_mode_enabled,
            "data_dir": str(current.data_dir),
            "results_db": str(store.db_path),
            "otel_endpoint": current.otel_exporter_otlp_endpoint,
            "runs_stored": store.count_runs(),
        }

    dist = web_dist if web_dist is not None else WEB_DIST
    if (dist / "index.html").exists():
        api.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @api.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "not found")
            return FileResponse(dist / "index.html")

    else:

        @api.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC / "runs.html")

        @api.get("/runs/{run_id}", include_in_schema=False)
        async def run_page(run_id: str) -> FileResponse:
            return FileResponse(STATIC / "run.html")

    return api


def app() -> FastAPI:
    settings = get_settings()
    log_provider_statuses(settings)
    store = ResultsStore(settings.results_db_path)
    # The API runs scenarios on its own copy of the mock services, separate from
    # the standalone service processes.
    cluster = ServiceCluster(settings.data_dir / "api-services", seed=settings.default_seed)
    engine = Engine(cluster, store, settings.data_dir / "api-control")
    # Built without network calls; health checks run only when asked for.
    pool = ProviderPool.from_settings(settings, httpx.AsyncClient())
    return create_app(store, engine, settings.web_dist, pool)
