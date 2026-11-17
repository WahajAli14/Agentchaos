"""Isolation and reproducibility: fixture reset, seeded IDs, no wall-clock leakage."""

import ast
import time
from pathlib import Path
from typing import Any

import pytest

from agentchaos.results.states import ExecutionState
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster
from agentchaos.services.fixtures import DEFAULT_FIXTURE
from agentchaos.workflows.order import DEFAULT_ORDER_INPUT, OrderWorkflow
from tests.conftest import SEED, run_workflow

SRC = Path(__file__).resolve().parents[1] / "src" / "agentchaos"


async def scenario(cluster: ServiceCluster, seed: int) -> dict[str, Any]:
    """A fixed sequence with duplicates, async commits and clock movement."""
    cluster.reset(seed)
    cluster.services["notifications"].flaws.async_commit_delay_ms = 250
    async with cluster.client() as client:
        ctx = await run_workflow(OrderWorkflow(), client, DEFAULT_ORDER_INPUT)
        cluster.runtime.scheduler.advance(100)
        await client.post(
            "http://billing/invoices", json={"order_id": ctx["order_id"], "amount_cents": 7500}
        )
        cluster.runtime.scheduler.advance(1000)
    return cluster.snapshot()


async def test_same_seed_gives_identical_database_state(cluster: ServiceCluster) -> None:
    first = await scenario(cluster, SEED)
    second = await scenario(cluster, SEED)
    assert first == second
    assert len(first["billing"]["invoices"]) == 2  # the scenario really did something


async def test_different_seed_changes_ids_only(cluster: ServiceCluster) -> None:
    a = await scenario(cluster, SEED)
    b = await scenario(cluster, SEED + 1)
    assert a["orders"]["orders"][0]["id"] != b["orders"]["orders"][0]["id"]
    assert [o["status"] for o in a["orders"]["orders"]] == [
        o["status"] for o in b["orders"]["orders"]
    ]


async def test_two_independent_clusters_agree(tmp_path: Path) -> None:
    snapshots = []
    for name in ("a", "b"):
        cluster = ServiceCluster(tmp_path / name, seed=SEED)
        snapshots.append(await scenario(cluster, SEED))
        cluster.close()
    assert snapshots[0] == snapshots[1]


async def test_reset_restores_the_fixture_exactly(cluster: ServiceCluster) -> None:
    pristine = cluster.snapshot()
    assert {r["sku"]: r["on_hand"] for r in pristine["inventory"]["stock"]} == dict(
        DEFAULT_FIXTURE.stock
    )
    dirty = await scenario(cluster, SEED)
    assert dirty != pristine
    cluster.services["billing"].flaws.async_commit_delay_ms = 10
    async with cluster.client() as client:
        await client.post("http://billing/invoices", json={"order_id": "o", "amount_cents": 1})
    assert cluster.runtime.scheduler.pending()

    cluster.reset(SEED)
    assert cluster.snapshot() == pristine
    assert cluster.runtime.clock.now_ms() == 0
    assert cluster.runtime.scheduler.pending() == []


async def test_service_reset_never_touches_the_results_database(
    cluster: ServiceCluster, store: ResultsStore
) -> None:
    store.create_run(
        "run_1",
        scenario="s",
        workflow="order_fulfilment",
        mode="deterministic",
        seed=SEED,
        fault_type=None,
        controls=[],
        logical_ms=0,
    )
    store.append_event("run_1", lane="agent", kind="tool_call", logical_ms=1, payload={"n": 1})
    before = store.db_path.read_bytes()
    for _ in range(3):
        await scenario(cluster, SEED)
        cluster.reset(SEED)
    assert store.db_path.read_bytes() == before
    assert store.count_runs() == 1
    assert store.current_state("run_1") is ExecutionState.RUNNING
    assert store.db_path.parent != cluster.services_dir


async def test_wall_clock_does_not_leak_into_service_state(
    cluster: ServiceCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails if anything in the services reads real time.

    Runs the same scenario with the wall clock skewed by ten years, and checks
    every stored timestamp is a small logical value rather than an epoch time.
    """
    baseline = await scenario(cluster, SEED)

    real_time, real_time_ns = time.time, time.time_ns
    skew = 10 * 365 * 24 * 3600
    monkeypatch.setattr(time, "time", lambda: real_time() + skew)
    monkeypatch.setattr(time, "time_ns", lambda: real_time_ns() + skew * 10**9)
    skewed = await scenario(cluster, SEED)
    assert skewed == baseline

    logical_now = cluster.runtime.clock.now_ms()
    stamps = [
        value
        for tables in baseline.values()
        for rows in tables.values()
        for row in rows
        for column, value in row.items()
        if column.endswith("_ms") and value is not None
    ]
    assert stamps, "expected timestamps in the snapshot"
    assert all(0 <= s <= logical_now for s in stamps)


FORBIDDEN_ATTRS: dict[str, set[str]] = {
    "time": {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "sleep"},
    "datetime": {"now", "utcnow", "today"},
    "date": {"today"},
    "uuid": {"uuid1", "uuid4"},
    "asyncio": {"sleep"},
    "os": {"urandom"},
    "func": {"now", "current_timestamp"},
}
FORBIDDEN_IMPORTS = {"time", "uuid", "secrets"}
# The results store stamps rows with the real time they were recorded.
# Live mode talks to a real LLM: it measures real latency and backs off in real time.
ALLOWED = {
    ("results/store.py", "func", "current_timestamp"),
    ("live_agent.py", "time", "perf_counter"),
    ("llm.py", "asyncio", "sleep"),
    ("llm_pool.py", "time", "monotonic"),
}
ALLOWED_IMPORTS = {("live_agent.py", "time"), ("llm_pool.py", "time")}


def test_source_has_no_wall_clock_or_unseeded_randomness() -> None:
    offences: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                base, attr = node.value.id, node.attr
                unseeded_random = base == "random" and attr != "Random"
                if (attr in FORBIDDEN_ATTRS.get(base, set()) or unseeded_random) and (
                    rel,
                    base,
                    attr,
                ) not in ALLOWED:
                    offences.append(f"{rel}:{node.lineno} {base}.{attr}")
            elif isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_IMPORTS:
                offences.append(f"{rel}:{node.lineno} from {node.module} import ...")
            elif isinstance(node, ast.Import):
                offences.extend(
                    f"{rel}:{node.lineno} import {alias.name}"
                    for alias in node.names
                    if alias.name in FORBIDDEN_IMPORTS and (rel, alias.name) not in ALLOWED_IMPORTS
                )
    assert offences == []
