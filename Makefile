.PHONY: setup dev api test lint bench live secrets

# macOS can mark the editable-install .pth file hidden, which Python 3.12 then
# skips; putting src on the path keeps `python -m agentchaos...` working anyway.
export PYTHONPATH := src
SEEDS ?= 20

setup:
	uv sync
	uv run pre-commit install

dev:
	docker compose up --build

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

test: lint
	uv run pytest

secrets:
	uv run pre-commit run gitleaks --all-files

api:
	uv run uvicorn agentchaos.api:app --factory --port 8100

# Runs {3 faults} x {6 control configs} x {SEEDS seeds} in deterministic mode and
# appends the runs to data/results/results.db. Same seeds give the same numbers.
bench:
	uv run python -m agentchaos.bench --seeds $(SEEDS)

# Live LLM smoke runs (needs a provider key in .env and AGENTCHAOS_LIVE_MODE_ENABLED=true).
RUNS ?= 3
live:
	uv run python -m agentchaos.live smoke --runs $(RUNS)
