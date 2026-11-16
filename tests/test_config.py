import logging
import subprocess
from pathlib import Path

import pytest

from agentchaos.config import Settings, log_provider_statuses, mask_secret

REPO = Path(__file__).resolve().parents[1]
FAKE_KEY = "gsk_test0000000000000000abcd1234"  # not a real key
ENV_KEYS = [
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "OLLAMA_BASE_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "AGENTCHAOS_ENV",
    "AGENTCHAOS_DEFAULT_SEED",
    "AGENTCHAOS_LIVE_MODE_ENABLED",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_mask_secret() -> None:
    assert mask_secret(FAKE_KEY) == "gsk_****1234"
    assert mask_secret("abcdefghijklmnop") == "****mnop"
    assert mask_secret("short") == "****"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_blank_keys_disable_providers_without_error(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("GROQ_API_KEY", "")
    clean_env.setenv("MISTRAL_API_KEY", "   ")
    statuses = {s.name: s for s in settings().provider_statuses()}
    assert not statuses["groq"].enabled
    assert statuses["groq"].reason == "disabled: no key"
    assert not statuses["mistral"].enabled
    assert not statuses["ollama"].enabled


def test_key_value_never_appears_in_repr_dump_or_logs(
    clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clean_env.setenv("GROQ_API_KEY", FAKE_KEY)
    s = settings()
    statuses = {st.name: st for st in s.provider_statuses()}
    assert statuses["groq"].enabled
    assert statuses["groq"].masked_key == "gsk_****1234"
    with caplog.at_level(logging.INFO, logger="agentchaos.config"):
        log_provider_statuses(s)
    rendered = "\n".join([repr(s), str(s), s.model_dump_json(), repr(statuses), caplog.text])
    assert FAKE_KEY not in rendered
    assert "provider mistral disabled: no key" in caplog.text


def test_defaults(clean_env: pytest.MonkeyPatch) -> None:
    s = settings()
    assert s.default_seed == 42
    assert s.live_mode_enabled is False
    assert s.results_db_path.name == "results.db"


def test_env_example_has_exactly_the_contract_keys() -> None:
    lines = (REPO / ".env.example").read_text().splitlines()
    keys = [line.split("=", 1)[0] for line in lines if line and not line.startswith("#")]
    assert keys == ENV_KEYS
    for line in lines:
        if line.endswith("_API_KEY=") or "_API_KEY=" not in line:
            continue
        pytest.fail(f".env.example must not contain a key value: {line.split('=')[0]}")


def test_dotenv_is_git_ignored_and_example_is_not() -> None:
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO, check=False)
    assert ignored.returncode == 0, ".env must be ignored"
    tracked_or_addable = subprocess.run(
        ["git", "add", "--dry-run", ".env.example"], cwd=REPO, check=False, capture_output=True
    )
    assert tracked_or_addable.returncode == 0, ".env.example must be committable"
