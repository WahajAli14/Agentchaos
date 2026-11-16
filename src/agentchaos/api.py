"""Read-only HTTP API over the results database, plus the minimal run-detail page.

    uvicorn agentchaos.api:app --factory

The page is a plain three-lane timeline built from stored events. It is the
P0 placeholder for the React UI (P1) and reads the same endpoints.
"""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from agentchaos.config import get_settings, log_provider_statuses
from agentchaos.metrics import benchmark_matrix
from agentchaos.results.store import ResultsStore

STATIC = Path(__file__).parent / "static"


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def mismatches(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Places where what the agent was told differs from what actually happened."""
    found: list[dict[str, Any]] = []
    for event in events:
        if event["kind"] == "fault_injected":
            found.append(
                {
                    "seq": event["seq"],
                    "logical_ms": event["logical_ms"],
                    "step": event["payload"]["step"],
                    "fault_type": event["payload"]["fault_type"],
                    "explanation": event["payload"]["explanation"],
                }
            )
    return found


def create_app(store: ResultsStore) -> FastAPI:
    api = FastAPI(title="agentchaos results")

    @api.get("/api/runs")
    async def list_runs(batch_id: str | None = None) -> list[dict[str, Any]]:
        return [_json_safe(run) for run in store.list_runs(batch_id)]

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

    @api.get("/api/benchmark")
    async def benchmark(batch_id: str | None = None) -> dict[str, Any]:
        batch = batch_id or store.latest_batch_id()
        if batch is None:
            return {"batch_id": None, "cells": []}
        return {
            "batch_id": batch,
            "cells": [cell.as_dict() for cell in benchmark_matrix(store, batch)],
        }

    @api.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "runs.html")

    @api.get("/runs/{run_id}")
    async def run_page(run_id: str) -> FileResponse:
        return FileResponse(STATIC / "run.html")

    return api


def app() -> FastAPI:
    settings = get_settings()
    log_provider_statuses(settings)
    return create_app(ResultsStore(settings.results_db_path))
