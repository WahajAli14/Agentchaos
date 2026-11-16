"""Metrics math, the benchmark runner, and the read-only API over stored runs."""

import re
from pathlib import Path

import httpx
import pytest

from agentchaos.api import create_app
from agentchaos.bench import ALL_CONTROLS, CONTROL_CONFIGS, run_benchmark
from agentchaos.engine import Engine, RunConfig
from agentchaos.faults import FAULT_TYPES
from agentchaos.metrics import (
    benchmark_matrix,
    controls_delta,
    controls_label,
    format_matrix,
    percentile,
)
from agentchaos.results.store import ResultsStore

STATIC = Path(__file__).resolve().parents[1] / "src" / "agentchaos" / "static"


def test_percentile_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([5.0], 95) == 5.0
    values = [float(n) for n in range(1, 21)]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
    assert percentile(values, 100) == 20.0
    assert percentile([3.0, 1.0, 2.0], 50) == 2.0


def test_controls_label() -> None:
    assert controls_label([]) == "none"
    assert controls_label(["saga", "idempotency"]) == "idempotency+saga"


async def test_matrix_counts_come_from_stored_runs(engine: Engine) -> None:
    for controls in ((), ("verify_after_write",)):
        for step in ("create_invoice", "reserve_inventory", "confirm_order"):
            await engine.run(
                RunConfig(
                    "m",
                    seed=42,
                    fault_type="timeout_after_commit",
                    target_step=step,
                    controls=controls,
                    batch_id="b1",
                )
            )
    await engine.run(RunConfig("other", seed=1, batch_id="b2"))

    cells = {c.controls: c for c in benchmark_matrix(engine.store, "b1")}
    off, on = cells["none"].as_dict(), cells["verify_after_write"].as_dict()
    assert (off["runs"], on["runs"]) == (3, 3)
    assert off["outcomes"]["undetected_violation"] == 2
    assert off["outcomes"]["verified_recovery"] == 1  # confirm is naturally idempotent
    assert off["verified_pct"] == pytest.approx(100 / 3)
    assert off["violations_by_type"] == {
        "reserved_stock_matches_confirmed_orders": 1,
        "single_invoice_per_confirmed_order": 1,
    }
    # Three retried requests reached a service; only two of them changed state.
    assert (off["duplicate_requests"], off["duplicate_committed_operations"]) == (3, 2)
    assert on["outcomes"]["verified_recovery"] == 3
    assert (on["duplicate_requests"], on["duplicate_committed_operations"]) == (0, 0)
    assert sum(off["outcomes"].values()) == off["runs"]
    assert len(off["run_ids"]) == 3  # every cell links to its runs

    delta = controls_delta(list(cells.values()), "timeout_after_commit", "verify_after_write")
    assert delta is not None
    assert delta["verified"] == 2
    assert delta["undetected_violation"] == -2
    assert delta["invariant_violations"] == -2
    assert delta["duplicate_committed_operations"] == -2
    assert controls_delta(list(cells.values()), "partial_commit", "saga") is None
    assert "timeout_after_commit" in format_matrix(list(cells.values()))


async def test_indeterminate_runs_are_their_own_column(engine: Engine) -> None:
    await engine.run(RunConfig("a", seed=1, async_delay_ms={"billing": 90_000}, batch_id="b"))
    await engine.run(RunConfig("a", seed=2, batch_id="b"))
    cell = benchmark_matrix(engine.store, "b")[0].as_dict()
    assert cell["outcomes"]["indeterminate"] == 1
    assert cell["outcomes"]["verified_recovery"] == 1
    assert cell["verified_pct"] == 50.0


async def test_benchmark_runs_the_full_matrix_and_appends(tmp_path: Path) -> None:
    batch_1, store = await run_benchmark(tmp_path, seeds=2, start_seed=42)
    expected = len(FAULT_TYPES) * len(CONTROL_CONFIGS) * 2
    cells = benchmark_matrix(store, batch_1)
    assert sum(c.runs for c in cells) == expected == store.count_runs()
    assert len(cells) == len(FAULT_TYPES) * len(CONTROL_CONFIGS)
    by_key = {(c.fault_type, c.controls): c for c in cells}
    all_on = controls_label(ALL_CONTROLS)
    for fault in FAULT_TYPES:
        # With all controls on, nothing is left undetected or violated.
        cell = by_key[(fault, all_on)].as_dict()
        assert cell["outcomes"]["undetected_violation"] == 0
        assert cell["invariant_violations"] == 0
    store.close()

    batch_2, store = await run_benchmark(tmp_path, seeds=2, start_seed=42)
    assert batch_2 != batch_1
    assert store.count_runs() == 2 * expected  # history kept, not replaced
    first = [c.as_dict() for c in benchmark_matrix(store, batch_1)]
    second = [c.as_dict() for c in benchmark_matrix(store, batch_2)]
    for a, b in zip(first, second, strict=True):
        a.pop("run_ids"), b.pop("run_ids")
        assert a == b  # same seeds reproduce the same numbers
    store.close()


async def test_api_serves_run_detail_with_three_lanes_and_mismatch(engine: Engine) -> None:
    summary = await engine.run(
        RunConfig(
            "api",
            seed=42,
            fault_type="timeout_after_commit",
            target_step="create_invoice",
            batch_id="b",
        )
    )
    transport = httpx.ASGITransport(app=create_app(engine.store))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        runs = (await client.get("/api/runs")).json()
        assert [r["run_id"] for r in runs] == [summary.run_id]
        assert runs[0]["classification"] == "undetected_violation"

        detail = (await client.get(f"/api/runs/{summary.run_id}")).json()
        assert {e["lane"] for e in detail["events"]} == {"agent", "proxy", "ground_truth"}
        assert detail["states"] == ["running", "resolved"]
        assert [m["fault_type"] for m in detail["mismatches"]] == ["timeout_after_commit"]
        assert detail["mismatches"][0]["step"] == "create_invoice"
        assert detail["outcome"]["agent_believed_outcome"] == "success"
        violated = [i["invariant"] for i in detail["invariants"] if not i["passed"]]
        assert violated == ["single_invoice_per_confirmed_order"]

        assert (await client.get("/api/runs/run_missing")).status_code == 404
        bench = (await client.get("/api/benchmark")).json()
        assert bench["batch_id"] == "b" and bench["cells"][0]["runs"] == 1
        for path in ("/", f"/runs/{summary.run_id}"):
            page = await client.get(path)
            assert page.status_code == 200 and "<table" in page.text


async def test_api_with_no_runs(store: ResultsStore) -> None:
    transport = httpx.ASGITransport(app=create_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        assert (await client.get("/api/runs")).json() == []
        assert (await client.get("/api/benchmark")).json() == {"batch_id": None, "cells": []}


def test_pages_follow_the_design_bans() -> None:
    for page in STATIC.glob("*.html"):
        text = page.read_text()
        assert "gradient" not in text and "blur" not in text and "box-shadow" not in text
        assert not re.search(r"border-radius:\s*([7-9]|\d{2,})px", text)
        assert not re.search("[\U0001f300-\U0001faff☀-➿]", text)
