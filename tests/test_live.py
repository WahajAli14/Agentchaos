"""LLM provider client, live agent loop and trace replay. No real provider is called."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from agentchaos.config import Settings
from agentchaos.engine import Engine, RunConfig, canonical_record
from agentchaos.live import replay_trace, run_live, save_trace
from agentchaos.live_agent import TOOLS, believed_outcome
from agentchaos.llm import (
    LLMError,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderConfig,
    ToolCall,
    billed_cost_usd,
    estimated_cost_usd,
    load_provider_configs,
    select_provider,
)
from agentchaos.results.states import Classification

FAKE_KEY = "gsk_test0000000000000000abcd1234"  # not a real key
PRICED = ProviderConfig(
    "groq",
    1,
    "test-model",
    True,
    True,
    base_url="https://llm.test/v1",
    key_env="GROQ_API_KEY",
    price_input_per_mtok=0.15,
    price_output_per_mtok=0.60,
)
UNPRICED = ProviderConfig("mistral", 2, "m", True, True, base_url="https://m.test/v1")
URL = "https://llm.test/v1/chat/completions"


def completion(tool: str | None = None, content: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [
            {"id": "c1", "type": "function", "function": {"name": tool, "arguments": '{"a": 1}'}}
        ]
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def provider(client: httpx.AsyncClient, sleeps: list[float]) -> OpenAICompatibleProvider:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return OpenAICompatibleProvider(
        PRICED, "https://llm.test/v1", SecretStr(FAKE_KEY), client, sleep=sleep
    )


# --- provider client -------------------------------------------------------


@respx.mock
async def test_chat_parses_tool_calls_and_usage_and_sends_bearer_key() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=completion("create_order")))
    async with httpx.AsyncClient() as client:
        reply = await provider(client, []).chat([{"role": "user", "content": "hi"}], [{"x": 1}])
    assert reply.tool_calls == (ToolCall("c1", "create_order", {"a": 1}),)
    assert (reply.prompt_tokens, reply.completion_tokens) == (100, 20)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(sent.content)
    assert body["model"] == "test-model" and body["tools"] == [{"x": 1}]


@respx.mock
async def test_retries_with_exponential_backoff_then_succeeds() -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, json=completion(content="done")),
        ]
    )
    sleeps: list[float] = []
    async with httpx.AsyncClient() as client:
        reply = await provider(client, sleeps).chat([], [])
    assert reply.content == "done" and reply.tool_calls == ()
    assert sleeps == [2.0, 4.0]


@respx.mock
async def test_gives_up_after_bounded_attempts_without_leaking_the_key() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    sleeps: list[float] = []
    async with httpx.AsyncClient() as client:
        llm = provider(client, sleeps)
        with pytest.raises(LLMError) as error:
            await llm.chat([], [])
    assert route.call_count == 6 and sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert "HTTP 503 after 6 attempts" in str(error.value)
    assert FAKE_KEY not in str(error.value) and FAKE_KEY not in repr(llm.__dict__)


@respx.mock
async def test_auth_error_is_not_retried() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(401, text="invalid api key"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(LLMError, match="HTTP 401"):
            await provider(client, []).chat([], [])
    assert route.call_count == 1


def test_costs_are_two_separate_fields() -> None:
    assert estimated_cost_usd(PRICED, 1_000_000, 500_000) == pytest.approx(0.15 + 0.30)
    assert billed_cost_usd(PRICED, 1_000_000, 500_000) == 0.0  # free tier: nothing charged
    assert estimated_cost_usd(UNPRICED, 1000, 1000) is None  # unknown, never guessed


def test_provider_file_has_no_secrets_and_groq_first() -> None:
    configs = load_provider_configs()
    assert configs[0].name == "groq"
    assert {c.name for c in configs} == {"groq", "mistral", "openrouter", "gemini", "ollama"}
    assert all(c.model for c in configs)


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in ("GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@respx.mock
async def test_selects_first_keyed_provider_else_ollama_else_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tags = respx.get("http://ollama.test:11434/api/tags")
    async with httpx.AsyncClient() as client:
        keyed = await select_provider(settings(monkeypatch, MISTRAL_API_KEY=FAKE_KEY), client)
        assert keyed is not None and keyed.name == "mistral"
        both = await select_provider(
            settings(monkeypatch, MISTRAL_API_KEY=FAKE_KEY, GROQ_API_KEY=FAKE_KEY), client
        )
        assert both is not None and both.name == "groq"

        tags.mock(return_value=httpx.Response(200, json={"models": []}))
        local = await select_provider(settings(monkeypatch), client)
        assert local is not None and local.name == "ollama"

        tags.mock(side_effect=httpx.ConnectError("down"))
        assert await select_provider(settings(monkeypatch), client) is None


def test_believed_outcome_parsing() -> None:
    assert believed_outcome("All done.\nRESULT: SUCCESS") == "success"
    assert believed_outcome("result: failed") == "failed"
    assert believed_outcome("I think it worked") == "unknown"
    assert believed_outcome(None) == "unknown"


def test_tool_schemas_are_valid_function_definitions() -> None:
    for tool in TOOLS:
        schema = tool.schema()
        assert schema["type"] == "function"
        assert schema["function"]["parameters"]["required"] == list(tool.properties)


# --- live agent loop with a scripted stand-in for the model ----------------


class NaiveModel:
    """Stands in for an LLM: follows the instructions and retries a failed call once."""

    name = "fake"
    model = "naive-v1"
    config = ProviderConfig(
        "fake", 1, "naive-v1", True, True, price_input_per_mtok=0.15, price_output_per_mtok=0.60
    )

    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.plan = [
            "create_order",
            "reserve_inventory",
            "create_invoice",
            "confirm_order",
            "send_notification",
        ]
        self.position = 0
        self.retried = False
        self.order: dict[str, Any] = {}
        self.calls = 0

    def _arguments(self, tool: str) -> dict[str, Any]:
        o, ids = self.order, self.ids
        return {
            "create_order": dict(o),
            "reserve_inventory": {
                "order_id": ids.get("ord"),
                "sku": o["sku"],
                "quantity": o["quantity"],
            },
            "create_invoice": {
                "order_id": ids.get("ord"),
                "amount_cents": o["quantity"] * o["unit_price_cents"],
            },
            "confirm_order": {
                "order_id": ids.get("ord"),
                "reservation_id": ids.get("res"),
                "invoice_id": ids.get("inv"),
            },
            "send_notification": {"order_id": ids.get("ord"), "message": "Order confirmed"},
        }[tool]

    async def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        last = messages[-1]
        if last["role"] == "user":
            self.order = json.loads(last["content"].split(": ", 1)[1])
        else:
            result = json.loads(last["content"])
            if "error" in result or result.get("status", 500) >= 300:
                if self.retried:
                    return self._say("Could not complete the order.\nRESULT: FAILED")
                self.retried = True
            else:
                self.retried = False
                body = result["body"]
                if isinstance(body, dict) and "id" in body:
                    self.ids[body["id"].split("_")[0]] = body["id"]
                self.position += 1
        if self.position >= len(self.plan):
            return self._say("Order processed.\nRESULT: SUCCESS")
        tool = self.plan[self.position]
        call = {
            "id": f"c{self.calls}",
            "type": "function",
            "function": {"name": tool, "arguments": json.dumps(self._arguments(tool))},
        }
        return LLMResponse(
            None,
            (ToolCall(f"c{self.calls}", tool, self._arguments(tool)),),
            200,
            30,
            {"role": "assistant", "content": None, "tool_calls": [call]},
        )

    @staticmethod
    def _say(text: str) -> LLMResponse:
        return LLMResponse(text, (), 200, 30, {"role": "assistant", "content": text})


def live_config(controls: tuple[str, ...] = (), fault: str = "timeout_after_commit") -> RunConfig:
    return RunConfig(
        "live",
        seed=42,
        fault_type=fault,
        target_step="create_invoice",
        controls=controls,
        mode="live",
    )


async def test_live_run_records_model_tokens_cost_and_real_latency(engine: Engine) -> None:
    model = NaiveModel()
    summary = await run_live(engine, model, live_config())
    # The same failure the scripted agent shows: retry after a lost response.
    assert summary.classification is Classification.UNDETECTED_VIOLATION
    run = engine.store.get_run(summary.run_id)
    outcome = engine.store.outcome(summary.run_id)
    assert run is not None and outcome is not None
    assert (run["mode"], run["provider"], run["model"]) == ("live", "fake", "naive-v1")
    assert outcome["llm_calls"] == model.calls == 7  # 6 tool turns + final message
    assert (outcome["prompt_tokens"], outcome["completion_tokens"]) == (1400, 210)
    assert outcome["billed_cost_usd"] == 0.0
    assert outcome["estimated_cost_usd"] == pytest.approx((1400 * 0.15 + 210 * 0.60) / 1e6)
    assert outcome["latency_ms"] is not None and outcome["latency_ms"] > 0
    assert outcome["agent_believed_outcome"] == "success"
    kinds = [e["kind"] for e in engine.store.events(summary.run_id) if e["lane"] == "agent"]
    assert kinds.count("llm_call") == 7 and "message" in kinds
    assert [t["tool"] for t in summary.trace if "tool" in t].count("create_invoice") == 2


async def test_live_run_with_controls_is_verified(engine: Engine) -> None:
    summary = await run_live(
        engine, NaiveModel(), live_config(("idempotency", "saga", "verify_after_write"))
    )
    assert summary.classification is Classification.VERIFIED_RECOVERY


async def test_live_agent_giving_up_triggers_saga(engine: Engine) -> None:
    summary = await run_live(engine, NaiveModel(), live_config(("saga",), "partial_commit"))
    assert summary.classification is Classification.PARTIAL_RECOVERY
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None and outcome["agent_believed_outcome"] == "failed"


async def test_bad_tool_calls_are_reported_to_the_model_not_executed(engine: Engine) -> None:
    class Confused(NaiveModel):
        async def chat(
            self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
        ) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    None,
                    (ToolCall("a", "drop_database", {}), ToolCall("b", "create_invoice", {})),
                    1,
                    1,
                    {"role": "assistant", "content": None},
                )
            assert "unknown tool" in messages[-2]["content"]
            assert "missing arguments" in messages[-1]["content"]
            return self._say("RESULT: FAILED")

    summary = await run_live(engine, Confused(), RunConfig("live", seed=1, mode="live"))
    outcome = engine.store.outcome(summary.run_id)
    assert outcome is not None and outcome["tool_calls"] == 0
    assert engine.cluster.ground_truth().orders == []


# --- replay ----------------------------------------------------------------


async def test_trace_replay_reproduces_the_live_run_without_an_llm(
    engine: Engine, tmp_path: Path
) -> None:
    model = NaiveModel()
    config = live_config()
    live = await run_live(engine, model, config, tmp_path / "traces")
    recorded = json.loads((tmp_path / "traces" / f"{live.run_id}.json").read_text())
    assert recorded["model"] == "naive-v1" and FAKE_KEY not in json.dumps(recorded)
    calls_before = model.calls

    first = await replay_trace(engine, recorded, ())
    second = await replay_trace(engine, recorded, ())
    assert model.calls == calls_before  # no LLM involved
    assert first.classification is live.classification is Classification.UNDETECTED_VIOLATION
    assert canonical_record(engine.store, first.run_id) == canonical_record(
        engine.store, second.run_id
    )
    run = engine.store.get_run(first.run_id)
    outcome = engine.store.outcome(first.run_id)
    assert run is not None and outcome is not None
    assert run["mode"] == "replay" and outcome["llm_calls"] == 0
    assert outcome["agent_believed_outcome"] == "success"

    # Same recorded decisions, controls on: the retry is no longer triggered.
    fixed = await replay_trace(engine, recorded, ("verify_after_write",))
    assert fixed.classification is Classification.VERIFIED_RECOVERY
    skipped = [e for e in engine.store.events(fixed.run_id) if e["kind"] == "replay_skipped"]
    assert len(skipped) == 1 and skipped[0]["payload"]["tool"] == "create_invoice"

    keyed = await replay_trace(engine, recorded, ("idempotency",))
    assert keyed.classification is Classification.VERIFIED_RECOVERY


async def test_save_trace_writes_one_file_per_run(engine: Engine, tmp_path: Path) -> None:
    model = NaiveModel()
    config = RunConfig("live", seed=3, mode="live")
    summary = await run_live(engine, model, config)
    path = save_trace(tmp_path, summary, config, model)
    assert path.name == f"{summary.run_id}.json"
    assert json.loads(path.read_text())["seed"] == 3


@respx.mock
async def test_429_waits_as_long_as_the_provider_says_capped() -> None:
    respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "12"}),
            httpx.Response(429, headers={"x-ratelimit-reset-tokens": "7.66s"}),
            httpx.Response(429, headers={"retry-after": "600"}),
            httpx.Response(429, headers={"x-ratelimit-reset-requests": "1m26.4s"}),
            httpx.Response(200, json=completion(content="done")),
        ]
    )
    sleeps: list[float] = []
    async with httpx.AsyncClient() as client:
        await provider(client, sleeps).chat([], [])
    # max(own backoff, provider hint), never more than 60 s per wait
    assert sleeps == [12.0, 7.66, 60.0, 60.0]


def test_retry_after_parsing() -> None:
    from agentchaos.llm import retry_after_s

    assert retry_after_s({"retry-after": "3"}) == 3.0
    assert retry_after_s({"x-ratelimit-reset-tokens": "250ms"}) == 0.25
    assert (
        retry_after_s({"x-ratelimit-reset-requests": "2s", "x-ratelimit-reset-tokens": "1m"})
        == 60.0
    )
    assert retry_after_s({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert retry_after_s({}) is None


async def test_provider_error_is_recorded_and_kept_out_of_model_stats(engine: Engine) -> None:
    from agentchaos.metrics import live_model_stats

    class Throttled(NaiveModel):
        async def chat(
            self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
        ) -> LLMResponse:
            if self.calls >= 2:
                raise LLMError("fake/naive-v1: HTTP 429 after 6 attempts")
            return await super().chat(messages, tools)

    aborted = await run_live(engine, Throttled(), RunConfig("live", seed=1, mode="live"))
    outcome = engine.store.outcome(aborted.run_id)
    assert outcome is not None and outcome["agent_believed_outcome"] == "provider_error"
    await run_live(engine, NaiveModel(), RunConfig("live", seed=2, mode="live"))
    [stats] = live_model_stats(engine.store)
    assert stats["runs"] == 1 and stats["provider_aborted_runs"] == 1
    assert stats["note"] == (
        "smoke test, n=1, not statistically meaningful; 1 more cut short by the provider, excluded"
    )
