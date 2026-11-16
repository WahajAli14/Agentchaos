from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from agentchaos.interfaces import Context, WorkflowSpec
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

if TYPE_CHECKING:
    from agentchaos.engine import Engine

SEED = 42


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[ServiceCluster]:
    cluster = ServiceCluster(tmp_path / "services", seed=SEED)
    cluster.reset(SEED)
    yield cluster
    cluster.close()


@pytest.fixture
async def client(cluster: ServiceCluster) -> AsyncIterator[httpx.AsyncClient]:
    async with cluster.client() as client:
        yield client


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ResultsStore]:
    store = ResultsStore(tmp_path / "results" / "results.db")
    yield store
    store.close()


async def run_workflow(
    spec: WorkflowSpec,
    client: httpx.AsyncClient,
    order_input: dict[str, Any],
    *,
    stop_after: str | None = None,
) -> Context:
    """Fault-free reference execution of a workflow spec (test helper, not the engine)."""
    ctx: Context = dict(order_input)
    for step in spec.steps:
        req = step.build(ctx)
        response = await client.request(
            req.method, f"http://{req.service}{req.path}", json=req.json, params=req.params
        )
        assert response.status_code < 300, (step.name, response.status_code, response.text)
        step.capture(ctx, response.json())
        if step.name == stop_after:
            break
    return ctx


@pytest.fixture
def engine(cluster: ServiceCluster, store: ResultsStore, tmp_path: Path) -> Iterator["Engine"]:
    from agentchaos.engine import Engine

    engine = Engine(cluster, store, tmp_path / "control")
    yield engine
    engine.close()
