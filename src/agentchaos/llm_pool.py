"""Provider pool: health checks, routing, client-side rate limits, circuit breakers.

Routing picks the highest-priority provider that is healthy, has tool support,
is under its rate limits and whose circuit is closed. One provider is chosen
per run and kept for the whole run, so a run's statistics always belong to a
single model. When a provider fails (after its own exponential backoff), its
breaker records the failure and the next selection fails over to the next
provider.
"""

import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from agentchaos.config import Settings, mask_secret
from agentchaos.llm import (
    LLMError,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderConfig,
    load_provider_configs,
)

logger = logging.getLogger("agentchaos.llm")
Now = Callable[[], float]

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Health probe. Call this tool.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
PROBE_MESSAGES = [{"role": "user", "content": "Call the ping tool."}]


class RateLimiter:
    """Sliding-window limit on requests per minute and per day."""

    def __init__(self, rpm: int | None, rpd: int | None, now: Now) -> None:
        self.rpm, self.rpd = rpm, rpd
        self._now = now
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()

    def _trim(self) -> None:
        current = self._now()
        while self._minute and current - self._minute[0] >= 60:
            self._minute.popleft()
        while self._day and current - self._day[0] >= 86_400:
            self._day.popleft()

    def allow(self) -> bool:
        self._trim()
        if self.rpm is not None and len(self._minute) >= self.rpm:
            return False
        return not (self.rpd is not None and len(self._day) >= self.rpd)

    def record(self) -> None:
        current = self._now()
        self._minute.append(current)
        self._day.append(current)

    def usage(self) -> dict[str, int | None]:
        self._trim()
        return {
            "used_last_minute": len(self._minute),
            "rpm": self.rpm,
            "used_last_day": len(self._day),
            "rpd": self.rpd,
        }


class CircuitBreaker:
    """Opens after consecutive failures; lets one trial call through after a cool-down."""

    def __init__(self, now: Now, failure_threshold: int = 3, reset_after_s: float = 60.0) -> None:
        self._now = now
        self._threshold = failure_threshold
        self._reset_after_s = reset_after_s
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._now() - self._opened_at >= self._reset_after_s:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self._threshold:
            self._opened_at = self._now()


@dataclass
class Member:
    provider: LLMProvider
    limiter: RateLimiter
    breaker: CircuitBreaker
    masked_key: str = ""
    healthy: bool = True
    disabled_reason: str | None = None
    health_checked: bool = False
    calls: int = 0
    failures: int = 0


@dataclass
class FailoverEvent:
    at: float
    skipped: str
    reason: str
    chosen: str | None


class RoutedProvider:
    """A pool member pinned for one run. Reports each call back to the pool."""

    def __init__(self, member: Member) -> None:
        self._member = member
        self.name = member.provider.name
        self.model = member.provider.model
        self.config: ProviderConfig = member.provider.config

    async def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        member = self._member
        if not member.limiter.allow():
            member.failures += 1
            raise LLMError(f"{self.name}/{self.model}: client-side rate limit reached")
        member.limiter.record()
        member.calls += 1
        try:
            reply = await member.provider.chat(messages, tools)
        except LLMError:
            member.failures += 1
            member.breaker.record_failure()
            raise
        member.breaker.record_success()
        return reply


@dataclass
class ProviderPool:
    members: list[Member]
    now: Now = time.monotonic
    disabled: dict[str, str] = field(default_factory=dict)
    failovers: list[FailoverEvent] = field(default_factory=list)

    @classmethod
    def from_providers(
        cls,
        providers: Sequence[LLMProvider],
        now: Now = time.monotonic,
        disabled: dict[str, str] | None = None,
        masked_keys: dict[str, str] | None = None,
    ) -> "ProviderPool":
        ordered = sorted(providers, key=lambda p: p.config.priority)
        members = [
            Member(
                provider,
                RateLimiter(provider.config.rpm, provider.config.rpd, now),
                CircuitBreaker(now),
                masked_key=(masked_keys or {}).get(provider.name, ""),
            )
            for provider in ordered
        ]
        return cls(members, now, dict(disabled or {}))

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        client: httpx.AsyncClient,
        configs: Sequence[ProviderConfig] | None = None,
    ) -> "ProviderPool":
        """Every provider with a key or URL in settings. Others are listed as disabled."""
        providers: list[LLMProvider] = []
        disabled: dict[str, str] = {}
        masked: dict[str, str] = {}
        for config in configs if configs is not None else load_provider_configs():
            if config.key_env is not None and config.base_url is not None:
                key = settings.provider_key(config.name)
                if key is None:
                    disabled[config.name] = "disabled: no key"
                    continue
                masked[config.name] = mask_secret(key.get_secret_value())
                providers.append(OpenAICompatibleProvider(config, config.base_url, key, client))
            elif config.base_url_env is not None:
                base = settings.ollama_base_url
                if base is None:
                    disabled[config.name] = "disabled: no base URL"
                    continue
                providers.append(
                    OpenAICompatibleProvider(config, f"{base.rstrip('/')}/v1", None, client)
                )
        for name, reason in disabled.items():
            logger.info("provider %s %s", name, reason)
        return cls.from_providers(providers, disabled=disabled, masked_keys=masked)

    async def health_check(self) -> None:
        """One tiny tool-calling probe per provider; disable any that fail, and say why."""
        for member in self.members:
            member.health_checked = True
            if not member.provider.config.supports_tools:
                member.healthy, member.disabled_reason = False, "disabled: no tool support"
                continue
            try:
                reply = await member.provider.chat(PROBE_MESSAGES, [PROBE_TOOL])
            except LLMError as error:
                member.healthy, member.disabled_reason = False, f"health check failed: {error}"
            else:
                if reply.tool_calls:
                    member.healthy, member.disabled_reason = True, None
                else:
                    member.healthy = False
                    member.disabled_reason = "health check failed: model did not call the tool"
            if not member.healthy:
                logger.warning(
                    "provider %s disabled: %s", member.provider.name, member.disabled_reason
                )

    def _skip_reason(self, member: Member) -> str | None:
        if not member.healthy:
            return member.disabled_reason or "unhealthy"
        if not member.provider.config.supports_tools:
            return "no tool support"
        if not member.breaker.allow():
            return "circuit open"
        if not member.limiter.allow():
            return "rate limited"
        return None

    def select(self) -> RoutedProvider | None:
        """The provider to use for the next run, or None if none is usable."""
        skipped: list[tuple[str, str]] = []
        chosen: Member | None = None
        for member in self.members:
            reason = self._skip_reason(member)
            if reason is None:
                chosen = member
                break
            skipped.append((member.provider.name, reason))
        for name, reason in skipped:
            self.failovers.append(
                FailoverEvent(self.now(), name, reason, chosen.provider.name if chosen else None)
            )
        return RoutedProvider(chosen) if chosen else None

    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [
            {
                "name": member.provider.name,
                "model": member.provider.model,
                "priority": member.provider.config.priority,
                "enabled": True,
                "healthy": member.healthy,
                "health_checked": member.health_checked,
                "reason": member.disabled_reason,
                "masked_key": member.masked_key,
                "supports_tools": member.provider.config.supports_tools,
                "circuit": member.breaker.state,
                "rate_limit": member.limiter.usage(),
                "calls": member.calls,
                "failures": member.failures,
            }
            for member in self.members
        ]
        rows.extend(
            {
                "name": name,
                "model": None,
                "priority": None,
                "enabled": False,
                "healthy": False,
                "health_checked": False,
                "reason": reason,
                "masked_key": "",
                "supports_tools": None,
                "circuit": None,
                "rate_limit": None,
                "calls": 0,
                "failures": 0,
            }
            for name, reason in self.disabled.items()
        )
        return rows

    def failover_log(self) -> list[dict[str, Any]]:
        return [
            {"skipped": e.skipped, "reason": e.reason, "chosen": e.chosen} for e in self.failovers
        ]
