.PHONY: setup dev api web test test-web e2e demo lint bench live secrets

# macOS can mark the editable-install .pth file hidden, which Python 3.12 then
# skips; putting src on the path keeps `python -m agentchaos...` working anyway.
export PYTHONPATH := src
SEEDS ?= 20

setup:
	uv sync
	uv run pre-commit install
	cd web && npm ci

dev:
	docker compose up --build

lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy

test: lint test-web
	uv run pytest

# Frontend: eslint, the design-rule lint, tsc and unit tests.
test-web:
	cd web && npm run lint && npm run typecheck && npm test

# Builds the UI, runs a real 20-seed benchmark into a temp dir, drives the UI
# with Playwright and refreshes docs/screenshots.
e2e:
	cd web && npm run e2e

web:
	cd web && npm run build

# Records the README demo GIF from a real run of the UI (needs ffmpeg).
demo:
	cd web && npm run demo

secrets:
	uv run pre-commit run gitleaks --all-files

api: web
	uv run uvicorn agentchaos.api:app --factory --port 8100

# Runs {3 faults} x {6 control configs} x {SEEDS seeds} in deterministic mode and
# appends the runs to data/results/results.db. Same seeds give the same numbers.
bench:
	uv run python -m agentchaos.bench --seeds $(SEEDS)

# Live LLM smoke runs (needs a provider key in .env and AGENTCHAOS_LIVE_MODE_ENABLED=true).
RUNS ?= 3
live:
	uv run python -m agentchaos.live smoke --runs $(RUNS)
