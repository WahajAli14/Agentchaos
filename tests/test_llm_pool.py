"""Provider pool: routing, failover, rate limiting, circuit breaker, health checks.

All providers are fakes. No real key or network is used.
"""

from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from agentchaos.config import Settings
from agentchaos.llm import LLMError, LLMResponse, ProviderConfig, ToolCall
from agentchaos.llm_pool import CircuitBreaker, ProviderPool, RateLimiter

FAKE_KEY = "gsk_test0000000000000000abcd1234"  # not a real key


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeProvider:
    def __init__(
        self,
        name: str,
        priority: int,
        *,
        rpm: int | None = None,
        rpd: int | None = None,
        tools: bool = True,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.config = ProviderConfig(name, priority, self.model, tools, True, rpm=rpm, rpd=rpd)
        self.failing = False
        self.calls_tool = True
        self.calls = 0

    async def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        self.calls += 1
        if self.failing:
            raise LLMError(f"{self.name}: HTTP 503 after 4 attempts")
        calls = (ToolCall("c", "ping", {}),) if self.calls_tool else ()
        return LLMResponse("ok", calls, 10, 2)


def pool_of(*providers: FakeProvider, clock: Clock | None = None) -> ProviderPool:
    return ProviderPool.from_providers(list(providers), now=clock or Clock())


def test_rate_limiter_enforces_per_minute_and_per_day_windows() -> None:
    clock = Clock()
    limiter = RateLimiter(rpm=2, rpd=3, now=clock)
    assert limiter.allow()
    limiter.record()
    limiter.record()
    assert not limiter.allow()  # minute window full
    clock.t = 61
    assert limiter.allow()
    limiter.record()
    clock.t = 200
    assert not limiter.allow()  # daily budget spent
    assert limiter.usage() == {"used_last_minute": 0, "rpm": 2, "used_last_day": 3, "rpd": 3}
    clock.t = 86_400 + 62
    assert limiter.allow()
    assert RateLimiter(None, None, clock).allow()


def test_circuit_breaker_opens_then_half_opens_then_closes_or_reopens() -> None:
    clock = Clock()
    breaker = CircuitBreaker(clock, failure_threshold=3, reset_after_s=60)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed" and breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    clock.t = 60
    assert breaker.state == "half_open" and breaker.allow()
    breaker.record_failure()  # the trial call failed
    assert breaker.state == "open"
    clock.t = 120
    breaker.record_success()
    assert breaker.state == "closed"


def test_routes_to_the_highest_priority_usable_provider() -> None:
    low, high = FakeProvider("mistral", 2), FakeProvider("groq", 1)
    pool = pool_of(low, high)
    chosen = pool.select()
    assert chosen is not None and (chosen.name, chosen.model) == ("groq", "groq-model")
    assert pool.failover_log() == []


async def test_failing_provider_trips_its_breaker_and_the_next_run_fails_over() -> None:
    clock = Clock()
    groq, mistral = FakeProvider("groq", 1), FakeProvider("mistral", 2)
    pool = pool_of(groq, mistral, clock=clock)
    groq.failing = True
    for _ in range(3):
        routed = pool.select()
        assert routed is not None and routed.name == "groq"
        with pytest.raises(LLMError):
            await routed.chat([], [])
    # Three consecutive failures: circuit open, the pool moves on.
    routed = pool.select()
    assert routed is not None and routed.name == "mistral"
    assert (await routed.chat([], [])).content == "ok"
    assert pool.failover_log() == [
        {"skipped": "groq", "reason": "circuit open", "chosen": "mistral"}
    ]
    status = {row["name"]: row for row in pool.status()}
    assert status["groq"]["circuit"] == "open" and status["groq"]["failures"] == 3
    assert status["mistral"]["calls"] == 1

    # After the cool-down the first provider gets one trial and recovers.
    groq.failing = False
    clock.t = 61
    routed = pool.select()
    assert routed is not None and routed.name == "groq"
    await routed.chat([], [])
    assert {row["name"]: row["circuit"] for row in pool.status()}["groq"] == "closed"


async def test_a_run_keeps_one_provider_so_models_are_never_mixed() -> None:
    groq, mistral = FakeProvider("groq", 1), FakeProvider("mistral", 2)
    pool = pool_of(groq, mistral)
    routed = pool.select()
    assert routed is not None
    groq.failing = True
    with pytest.raises(LLMError):
        await routed.chat([], [])
    assert mistral.calls == 0  # no silent mid-run switch to another model


async def test_rate_limit_waits_mid_run_and_is_skipped_between_runs() -> None:
    clock = Clock()
    groq, mistral = FakeProvider("groq", 1, rpm=2, rpd=4), FakeProvider("mistral", 2)
    pool = pool_of(groq, mistral, clock=clock)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.t += seconds

    pool.sleep = sleep
    routed = pool.select()
    assert routed is not None and routed.name == "groq"
    clock.t = 10
    await routed.chat([], [])
    clock.t = 20
    await routed.chat([], [])
    # Minute budget full: the third call waits until the t=10 slot expires (t=70).
    await routed.chat([], [])
    assert slept == [50.0] and groq.calls == 3
    # Between runs, a provider at its minute limit is skipped, not waited on.
    after = pool.select()
    assert after is not None and after.name == "mistral"
    assert pool.failover_log()[-1] == {
        "skipped": "groq",
        "reason": "rate limited",
        "chosen": "mistral",
    }
    clock.t += 61
    back = pool.select()
    assert back is not None and back.name == "groq"
    await back.chat([], [])
    # Daily budget spent: fail instead of waiting a day.
    with pytest.raises(LLMError, match="daily request limit"):
        await back.chat([], [])
    assert groq.calls == 4


async def test_health_check_disables_failing_and_toolless_providers_with_reasons() -> None:
    down, mute, no_tools, good = (
        FakeProvider("groq", 1),
        FakeProvider("mistral", 2),
        FakeProvider("openrouter", 3, tools=False),
        FakeProvider("gemini", 4),
    )
    down.failing = True
    mute.calls_tool = False
    pool = pool_of(down, mute, no_tools, good)
    await pool.health_check()
    status = {row["name"]: row for row in pool.status()}
    assert status["groq"]["healthy"] is False
    assert "health check failed: groq: HTTP 503" in status["groq"]["reason"]
    assert status["mistral"]["reason"] == "health check failed: model did not call the tool"
    assert status["openrouter"]["reason"] == "disabled: no tool support"
    assert no_tools.calls == 0  # not probed
    assert status["gemini"]["healthy"] is True and status["gemini"]["reason"] is None
    chosen = pool.select()
    assert chosen is not None and chosen.name == "gemini"
    assert [e["skipped"] for e in pool.failover_log()] == ["groq", "mistral", "openrouter"]


async def test_no_usable_provider_returns_none() -> None:
    only = FakeProvider("groq", 1)
    only.failing = True
    pool = pool_of(only)
    await pool.health_check()
    assert pool.select() is None
    assert pool.failover_log()[-1]["chosen"] is None


async def test_pool_from_settings_lists_keyless_providers_as_disabled_and_masks_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    async with httpx.AsyncClient() as client:
        pool = ProviderPool.from_settings(settings, client)
    status = {row["name"]: row for row in pool.status()}
    assert set(status) == {"groq", "mistral", "openrouter", "gemini", "ollama"}
    assert status["groq"]["enabled"] and status["groq"]["masked_key"] == "gsk_****1234"
    assert status["ollama"]["enabled"] and status["ollama"]["masked_key"] == ""
    for name in ("mistral", "openrouter", "gemini"):
        assert status[name]["enabled"] is False and status[name]["reason"] == "disabled: no key"
    assert FAKE_KEY not in repr(pool.status()) and FAKE_KEY not in repr(pool)
