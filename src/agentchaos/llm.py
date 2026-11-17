"""LLM providers behind one interface.

One OpenAI-compatible client covers every provider. P1 uses the first provider
with a key (by priority, Groq first), falling back to a local Ollama if it
answers. API keys are read from settings and only ever sent in the
Authorization header: never logged, stored or put in an exception message.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml
from pydantic import SecretStr

from agentchaos.config import Settings

logger = logging.getLogger("agentchaos.llm")
PROVIDERS_FILE = Path(__file__).parent / "providers.yaml"
RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    priority: int
    model: str
    supports_tools: bool
    free_tier: bool
    base_url: str | None = None
    key_env: str | None = None
    base_url_env: str | None = None
    rpm: int | None = None
    rpd: int | None = None
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None


def load_provider_configs(path: Path = PROVIDERS_FILE) -> list[ProviderConfig]:
    raw = yaml.safe_load(path.read_text())["providers"]
    configs = []
    for name, entry in raw.items():
        price = entry.get("price_per_mtok_usd") or {}
        configs.append(
            ProviderConfig(
                name=name,
                priority=entry["priority"],
                model=entry["model"],
                supports_tools=entry["supports_tools"],
                free_tier=entry["free_tier"],
                base_url=entry.get("base_url"),
                key_env=entry.get("key_env"),
                base_url_env=entry.get("base_url_env"),
                rpm=entry.get("rpm"),
                rpd=entry.get("rpd"),
                price_input_per_mtok=price.get("input"),
                price_output_per_mtok=price.get("output"),
            )
        )
    return sorted(configs, key=lambda c: c.priority)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    prompt_tokens: int
    completion_tokens: int
    # The assistant message as returned, to append to the conversation.
    message: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """A provider call failed after retries. Never contains a key."""


class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def config(self) -> ProviderConfig: ...

    async def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse: ...


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ProviderConfig,
        base_url: str,
        api_key: SecretStr | None,
        client: httpx.AsyncClient,
        *,
        max_attempts: int = 6,
        backoff_s: float = 2.0,
        max_wait_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.name = config.name
        self.model = config.model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client
        self._max_attempts = max_attempts
        self._backoff_s = backoff_s
        self._max_wait_s = max_wait_s
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    async def chat(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": 0,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"
        last = "no attempt made"
        retry_after: float | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                    timeout=60.0,
                )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}"
            else:
                if response.status_code == 200:
                    return self._parse(response.json())
                last = f"HTTP {response.status_code}"
                retry_after = retry_after_s(response.headers)
                if response.status_code not in RETRYABLE:
                    detail = response.text[:300]
                    raise LLMError(f"{self.name}/{self.model}: {last}: {detail}")
            if attempt < self._max_attempts:
                # The provider knows when its limit resets; trust it over our guess.
                backoff = self._backoff_s * 2 ** (attempt - 1)
                await self._sleep(min(self._max_wait_s, max(backoff, retry_after or 0.0)))
                retry_after = None
        raise LLMError(f"{self.name}/{self.model}: {last} after {self._max_attempts} attempts")

    @staticmethod
    def _parse(data: dict[str, Any]) -> LLMResponse:
        message = data["choices"][0]["message"]
        calls = tuple(
            ToolCall(
                id=call.get("id") or f"call_{index}",
                name=call["function"]["name"],
                arguments=parse_arguments(call["function"].get("arguments")),
            )
            for index, call in enumerate(message.get("tool_calls") or [])
        )
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            message={
                "role": "assistant",
                "content": message.get("content"),
                **({"tool_calls": message["tool_calls"]} if message.get("tool_calls") else {}),
            },
        )


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def _duration_s(value: str) -> float | None:
    """Parse '12', '7.66s', '1m26.4s' or '250ms' into seconds."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    match = _DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    minutes, seconds, millis = (float(g) if g else 0.0 for g in match.groups())
    return minutes * 60 + seconds + millis / 1000


def retry_after_s(headers: Mapping[str, str]) -> float | None:
    """How long the provider says to wait: Retry-After, else the longest rate-limit reset."""
    if "retry-after" in headers:
        return _duration_s(headers["retry-after"])
    resets = [
        _duration_s(headers[name])
        for name in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")
        if name in headers
    ]
    known = [r for r in resets if r is not None]
    return max(known) if known else None


def estimated_cost_usd(
    config: ProviderConfig, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Token-equivalent cost at the public list price, or None if no price is known."""
    if config.price_input_per_mtok is None or config.price_output_per_mtok is None:
        return None
    return (
        prompt_tokens * config.price_input_per_mtok
        + completion_tokens * config.price_output_per_mtok
    ) / 1_000_000


def billed_cost_usd(config: ProviderConfig, prompt_tokens: int, completion_tokens: int) -> float:
    """What was actually charged: zero on a free tier."""
    if config.free_tier:
        return 0.0
    return estimated_cost_usd(config, prompt_tokens, completion_tokens) or 0.0


async def _ollama_responds(base_url: str, client: httpx.AsyncClient) -> bool:
    try:
        response = await client.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def select_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    configs: Sequence[ProviderConfig] | None = None,
) -> LLMProvider | None:
    """First keyed provider by priority; otherwise Ollama if it answers; otherwise None."""
    configs = configs if configs is not None else load_provider_configs()
    for config in configs:
        if not config.supports_tools or config.key_env is None or config.base_url is None:
            continue
        key = settings.provider_key(config.name)
        if key is not None:
            return OpenAICompatibleProvider(config, config.base_url, key, client)
        logger.info("provider %s disabled: no key", config.name)
    for config in configs:
        if config.base_url_env is None or not config.supports_tools:
            continue
        base = settings.ollama_base_url
        if base and await _ollama_responds(base, client):
            return OpenAICompatibleProvider(config, f"{base.rstrip('/')}/v1", None, client)
        logger.info("provider %s disabled: %s not reachable", config.name, base or "no base URL")
    return None
