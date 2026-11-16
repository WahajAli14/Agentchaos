.PHONY: setup dev test lint bench secrets

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

bench:
	@echo "make bench: the benchmark runner is a Day 3 deliverable and does not exist yet." >&2
	@exit 1
