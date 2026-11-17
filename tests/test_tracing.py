"""Tracing: spans per run and per proxied call, with no secrets in attributes."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentchaos import tracing
from agentchaos.engine import Engine, RunConfig, canonical_record
from agentchaos.live import run_live
from tests.test_live import FAKE_KEY, NaiveModel


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.configure(provider)
    yield exporter
    tracing.configure(None)


CONFIG = RunConfig(
    "trace",
    seed=42,
    fault_type="timeout_after_commit",
    target_step="create_invoice",
    controls=("idempotency",),
)


async def test_one_trace_per_run_with_a_span_per_proxied_call(
    engine: Engine, spans: InMemorySpanExporter
) -> None:
    summary = await engine.run(CONFIG)
    finished = spans.get_finished_spans()
    root = next(s for s in finished if s.name == "agentchaos.run")
    calls = [s for s in finished if s.name == "agentchaos.proxy.send"]
    assert root.attributes is not None
    assert root.attributes["agentchaos.run_id"] == summary.run_id
    assert root.attributes["agentchaos.classification"] == "verified_recovery"
    assert root.attributes["agentchaos.controls"] == "idempotency"
    assert len(calls) == 6  # five steps plus the retried invoice
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in calls)
    faulted = [s for s in calls if s.attributes and s.attributes["agentchaos.fault_injected"]]
    assert len(faulted) == 1
    assert faulted[0].attributes is not None
    assert faulted[0].attributes["agentchaos.step"] == "create_invoice"
    assert faulted[0].attributes["agentchaos.timed_out"] is True
    # Request headers (the idempotency key) are never recorded on spans.
    rendered = repr([dict(s.attributes or {}) for s in finished])
    assert "idem_" not in rendered and "Idempotency" not in rendered


async def test_llm_spans_carry_model_and_tokens_but_no_key_or_content(
    engine: Engine, spans: InMemorySpanExporter
) -> None:
    await run_live(engine, NaiveModel(), RunConfig("live", seed=42, mode="live"))
    llm = [s for s in spans.get_finished_spans() if s.name == "agentchaos.llm.chat"]
    assert len(llm) == 6
    assert llm[0].attributes is not None
    assert llm[0].attributes["gen_ai.request.model"] == "naive-v1"
    assert llm[0].attributes["gen_ai.usage.input_tokens"] == 200
    rendered = repr([dict(s.attributes or {}) for s in spans.get_finished_spans()])
    assert FAKE_KEY not in rendered and "cust_acme" not in rendered


async def test_tracing_does_not_change_the_canonical_record(
    engine: Engine, spans: InMemorySpanExporter
) -> None:
    traced = await engine.run(CONFIG)
    tracing.configure(None)
    untraced = await engine.run(CONFIG)
    assert canonical_record(engine.store, traced.run_id) == canonical_record(
        engine.store, untraced.run_id
    )


def test_setup_is_off_without_an_endpoint_or_a_reachable_collector() -> None:
    assert tracing.setup_tracing(None) is False
    assert tracing.setup_tracing("http://127.0.0.1:1") is False
    assert isinstance(tracing.get_tracer().start_span("x").get_span_context().trace_id, int)
