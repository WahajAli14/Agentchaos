# AgentChaos

AI integration reliability lab. It measures how an LLM agent breaks business
correctness when the APIs it calls fail, and whether specific safety controls fix it.

**Status: Day 1 of 6.** The mock services, deterministic runtime, invariant checker
and results store exist. There is no fault proxy, agent, safety control, benchmark
or UI yet, and therefore no results to report.

## What exists

- Four mock services (`orders`, `inventory`, `billing`, `notifications`), each with
  its own SQLite database and three configurable flaws: no idempotency support,
  duplicates accepted, and async commit (answers `202` before anything is stored).
- A logical clock, a deterministic event scheduler and seeded ID generation. No
  service reads wall-clock time.
- A ground-truth invariant checker that reads the service databases directly.
  API responses are treated as observations, not truth.
- An append-only results database. UPDATE and DELETE are rejected by triggers.

## Run it

```
make setup     # uv sync + install the gitleaks pre-commit hook
make test      # ruff, mypy, pytest
make dev       # docker compose up: services on ports 8101-8104
```

Copy `.env.example` to `.env`. API keys are not needed until live mode (Day 4).

Design choices are logged in [DECISIONS.md](DECISIONS.md).
