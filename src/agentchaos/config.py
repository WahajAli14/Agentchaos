"""Configuration loaded from `.env`.

Key values are held as `SecretStr` and must never be printed, logged, returned
from an API or written to a trace. Use `mask_secret` if a key has to be shown.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("agentchaos.config")

KEYED_PROVIDERS = ("groq", "mistral", "openrouter", "gemini")


def mask_secret(value: str | None) -> str:
    """Render a secret for display, e.g. `gsk_abcdef1234` -> `gsk_****1234`."""
    if not value:
        return ""
    if len(value) < 12:
        return "****"
    prefix = ""
    head, sep, _ = value.partition("_")
    if sep and len(head) <= 4:
        prefix = head + sep
    return f"{prefix}****{value[-4:]}"


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    enabled: bool
    reason: str
    masked_key: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    groq_api_key: SecretStr | None = Field(default=None, alias="GROQ_API_KEY")
    mistral_api_key: SecretStr | None = Field(default=None, alias="MISTRAL_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    ollama_base_url: str | None = Field(default=None, alias="OLLAMA_BASE_URL")

    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )

    env: str = Field(default="dev", alias="AGENTCHAOS_ENV")
    default_seed: int = Field(default=42, alias="AGENTCHAOS_DEFAULT_SEED")
    live_mode_enabled: bool = Field(default=False, alias="AGENTCHAOS_LIVE_MODE_ENABLED")
    # Not part of `.env`; optional override used by Docker Compose and tests.
    data_dir: Path = Field(default=Path("data"), alias="AGENTCHAOS_DATA_DIR")
    # Built frontend to serve; defaults to web/dist next to the source tree.
    web_dist: Path | None = Field(default=None, alias="AGENTCHAOS_WEB_DIST")

    @field_validator(
        "groq_api_key",
        "mistral_api_key",
        "openrouter_api_key",
        "gemini_api_key",
        "ollama_base_url",
        "otel_exporter_otlp_endpoint",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def provider_key(self, name: str) -> SecretStr | None:
        key: SecretStr | None = getattr(self, f"{name}_api_key")
        return key

    def provider_statuses(self) -> list[ProviderStatus]:
        statuses = []
        for name in KEYED_PROVIDERS:
            key = self.provider_key(name)
            if key is None:
                statuses.append(ProviderStatus(name, False, "disabled: no key", ""))
            else:
                statuses.append(
                    ProviderStatus(name, True, "key present", mask_secret(key.get_secret_value()))
                )
        if self.ollama_base_url is None:
            statuses.append(ProviderStatus("ollama", False, "disabled: no base URL", ""))
        else:
            statuses.append(ProviderStatus("ollama", True, "base URL present", ""))
        return statuses

    @property
    def services_dir(self) -> Path:
        return self.data_dir / "services"

    @property
    def results_db_path(self) -> Path:
        return self.data_dir / "results" / RESULTS_DB_FILENAME


RESULTS_DB_FILENAME = "results.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def log_provider_statuses(settings: Settings) -> None:
    """Log each provider's enabled/disabled state once. Never logs key values."""
    for status in settings.provider_statuses():
        if status.enabled:
            logger.info("provider %s enabled (%s)", status.name, status.reason)
        else:
            logger.info("provider %s %s", status.name, status.reason)
