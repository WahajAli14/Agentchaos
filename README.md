# AgentChaos

**Will your business systems survive an autonomous AI agent?**

AgentChaos is a reliability lab for AI integrations. It runs an agent through an
order workflow, injects the failures real APIs produce, and then checks the
services' databases to see what actually happened. It measures how often
business correctness breaks, and whether three specific safety controls fix it.

![Run timeline: the agent is told TIMEOUT, the database shows the invoice was created, the retry creates a second one](docs/screenshots/run-detail-dark.png)

The agent above was told `TIMEOUT`, retried, and reported success. The billing
database holds two invoices for one order. Nothing in the run noticed.

![Demo](docs/demo.gif)

## The problem

An agent that calls business APIs treats responses as facts. They are not:

- A timeout can mean the request succeeded.
- A `200` can mean the work is still queued.
- A successful retry can mean the first attempt also went through.

The usual agent behaviour, "it failed, call it again", turns each of these into
duplicate invoices, double-reserved stock or orders left half done. The agent's
own account of the run does not show it.

## What it does

One workflow: create order → reserve inventory → create invoice → confirm order → notify.

```mermaid
flowchart LR
  A[Agent<br/>scripted, LLM, or replayed trace] --> C[Safety controls<br/>idempotency · saga · verify-after-write]
  C --> P[Fault proxy<br/>seeded, every injection logged]
  P --> O[(orders)]
  P --> I[(inventory)]
  P --> B[(billing)]
  P --> N[(notifications)]
  O & I & B & N --> G[Invariant checker<br/>reads the databases directly]
  G --> K[Classifier]
  K --> R[(Results DB<br/>append-only)]
  R --> U[API + UI]
```

- **Mock services.** Four FastAPI apps, each with its own SQLite database, with
  switchable flaws: idempotency keys ignored, duplicates accepted, and async
  commit (answers `202` before anything is stored).
- **Fault proxy.** Three faults, chosen and placed by a seeded RNG:
  - `timeout_after_commit`: the request commits, the response is lost.
  - `duplicate_delivery`: the request is delivered twice, the agent sees one response.
  - `partial_commit`: earlier steps commit, then the next service is down for every attempt.
- **Ground truth.** After every run, five invariants are checked against the
  databases, never against API responses.
- **Safety controls,** toggled per run:
  - *Idempotency keys*, with a client-side dedupe store and server-side support in the mocks.
  - *Saga*: on failure, compensate completed steps in reverse order.
  - *Verify-after-write*: after a timeout or 5xx, query actual state before retrying.
- **Results.** Every run is stored in an append-only database. UPDATE and DELETE
  are rejected by triggers. Every number in the UI and in this README is
  computed from stored runs.

### How a run is judged

Execution state and classification are separate.

| Execution state | Meaning |
|---|---|
| `running` → `resolved` | The final state was read from the databases. |
| `pending_reconciliation` | A service accepted a write it has not committed yet. Bounded, logged attempts follow. |
| `indeterminate` | Still unknown after the bounded attempts. Reported as its own category and never classified. |

| Classification (resolved runs only) | Meaning |
|---|---|
| `verified_recovery` | Order confirmed, every invariant holds, no duplicate operation committed. |
| `partial_recovery` | Invariants hold, but the order was not fulfilled, or a duplicate side effect was left behind. |
| `manual_intervention_required` | A control checked actual state and escalated to a person. Never counted as a success. |
| `undetected_violation` | An invariant is violated and nothing in the run detected it. |

## Results

Deterministic mode, scripted naive-retry agent, 20 seeds (42–61) per cell,
360 runs. Reproduce with `make bench`; the same seeds give the same numbers.

Runs ending `verified_recovery`, out of 20:

| Controls | `timeout_after_commit` | `duplicate_delivery` | `partial_commit` |
|---|---|---|---|
| none | 4 | 4 | 0 |
| idempotency keys, server ignores them | 4 | 4 | 0 |
| idempotency keys, server honours them | **20** | **20** | 0 |
| verify-after-write | **20** | 4 | 0 |
| saga | 4 | 4 | 0 |
| all three | **20** | **20** | 0 |

Controls off against all three on:

| Fault | Undetected-violation runs | Invariant violations | Duplicate operations committed |
|---|---|---|---|
| `timeout_after_commit` | 7 → 0 | 7 → 0 | 16 → 0 |
| `duplicate_delivery` | 7 → 0 | 7 → 0 | 16 → 0 |
| `partial_commit` | 16 → 0 | 32 → 0 | 0 → 0 |

![Benchmark matrix](docs/screenshots/benchmark-dark.png)

### Every remaining failure, explained

- **Controls off, 4 of 20 still verify under timeout and duplicate faults.**
  In those seeds the fault hit `confirm_order`, which is naturally idempotent:
  confirming a confirmed order changes nothing. The duplicate request still
  reached the service; it is counted under duplicate requests, not duplicate
  committed operations.
- **Controls off, 9 of 20 are `partial_recovery`, not violations.** The fault
  hit `create_order` or `notify_customer`. The duplicate (a stray unconfirmed
  order, or a second notification) breaks none of the five invariants, but it
  is real leftover state, so the run is not reported as a clean recovery.
- **Idempotency keys the server ignores change nothing.** Identical to no
  controls in all three columns. Idempotency needs server cooperation and
  durable state; a client-side store cannot help when the response was lost,
  because the client has nothing to remember.
- **Verify-after-write does not fix `duplicate_delivery` (4 of 20).** The
  duplicate arrives behind a normal `201`, so there is no ambiguous response to
  trigger a check.
- **Saga alone fixes neither timeout nor duplicate faults.** It only acts when
  the workflow fails, and it can only compensate writes the agent knows about.
- **`partial_commit` never reaches `verified_recovery`, with any controls.**
  The service is down for every attempt, so the order cannot complete. What
  changes is how it ends:
  - no controls: 16 `undetected_violation` (reservation and sometimes invoice
    stranded) and 4 `partial_recovery` (the outage hit the first step after
    order creation, leaving only an unconfirmed order);
  - verify-after-write alone: 20 `manual_intervention_required`. It confirms
    from actual state which writes are still live and escalates. The databases
    are still wrong (32 violations); a person has to act;
  - saga: 20 `partial_recovery`. Compensations release the reservation, void
    the invoice and cancel the order. The state is consistent and the order is
    not fulfilled.
- **Compensation is not rollback.** After a saga the original rows still exist,
  marked `released`, `void` and `cancelled`.
- **Indeterminate runs: 0 in this benchmark.** The async flaw is off in the
  matrix. With it on and a commit slower than the reconciliation budget, a run
  ends `indeterminate` and is never classified (covered by tests).

## Live LLM agent

Live mode runs a real tool-calling model against the same proxy and controls.
One OpenAI-compatible client covers Groq, Mistral, OpenRouter, Gemini and a
local Ollama. Model ids, limits and list prices live in
`src/agentchaos/providers.yaml`, not in code.

A provider pool routes each run to the highest-priority provider that is
healthy, under its client-side rate limit and has a closed circuit breaker. A
startup probe makes one small tool call per provider and disables any that
fail, with the reason. One provider is kept for a whole run, so a run's
numbers always belong to a single model; a failing provider is skipped at the
next run. API keys are only ever shown masked.

```
AGENTCHAOS_LIVE_MODE_ENABLED=true   # in .env, plus one provider key
make live RUNS=3
```

### Live results: Groq `openai/gpt-oss-120b`

**Smoke test, n=3 per scenario, not statistically meaningful.** Batch
`live_000381`, 18 runs, seeds 42–44, all controls off and all three on. Every
run completed; none was cut short by the provider.

| Fault | Controls | Outcomes (from the databases) | What the agent reported |
|---|---|---|---|
| `timeout_after_commit` | none | 3 `undetected_violation` | success ×3 |
| `timeout_after_commit` | all three | 3 `verified_recovery` | success ×3 |
| `duplicate_delivery` | none | 3 `undetected_violation` | success ×3 |
| `duplicate_delivery` | all three | 3 `verified_recovery` | success ×3 |
| `partial_commit` | none | 2 `undetected_violation`, 1 `partial_recovery` | failed ×2, unknown ×1 |
| `partial_commit` | all three | 3 `partial_recovery` | failed ×3 |

What the model actually did:

- **Lost response:** after the timeout it called the same tool again in all
  three runs, exactly like the scripted naive agent. One duplicate invoice or
  reservation committed each time, and it reported `RESULT: SUCCESS`.
- **Duplicate delivery:** it saw one normal `201` and had no reason to suspect
  anything. The duplicate committed and it reported success.
- **Outage at `confirm_order`, controls off:** the model improvised. It retried,
  released the reservation and reserved again, voided the invoice and issued a
  new one, then cancelled the order. 16–20 tool calls, and 2–3 of those writes
  committed as duplicates nobody undid. Its own recovery attempt caused the
  violation. Up to 33k tokens per run, against about 6k for a clean run.
- **Outage at `reserve_inventory`:** it retried, looked the order up, and
  cancelled it. Consistent state, order not fulfilled (`partial_recovery`).
- **All controls on:** the same improvisation happened (up to 4 duplicate
  requests reached the services), but server-side idempotency replayed them, so
  0 duplicate operations committed and every run ended consistent.

Cost: $0 billed (free tier). Estimated at the list price configured in
`providers.yaml`: $0.0010–0.0061 per run. Wall-clock agent time 4–118 s per run,
mostly model latency.

An earlier batch (`live_000363`) is kept in the results database but not
reported: 6 of its 18 runs were ended by Groq rate limiting (HTTP 429), which
says nothing about the agent. The client now honours `Retry-After`, and any
run the provider still cuts short is recorded as `provider_error` and left out
of per-model statistics.

Every live run saves its tool-call trace to `data/traces/`. A trace can be
replayed without an LLM, with different controls:

```
uv run python -m agentchaos.live replay data/traces/run_000123.json --controls all
```

Cost is two fields: `billed_cost_usd` (what was charged; 0 on a free tier) and
`estimated_cost_usd` (tokens at the public list price, or unknown if no price
is configured in `providers.yaml`).

## Screens

Scenarios, Runs (with the three-lane timeline), Benchmark, Invariants,
Providers and Settings. Dark and light themes; screenshots of each are in
`docs/screenshots/`. Keyboard: `j`/`k` move, `Enter` opens, `r` runs the
selected scenario, `/` focuses the filter, `?` lists shortcuts.

## Tracing

With `docker compose up`, every run is exported as one OpenTelemetry trace to
Jaeger at http://localhost:16686 (service `agentchaos`): a root span per run
and a child span per proxied call and per LLM call. Spans carry step names,
status codes and token counts. They do not carry headers, message contents or
keys. Tracing turns itself off if no collector is reachable.

## Run it

```
cp .env.example .env
docker compose up --build     # UI and API on http://localhost:8100, Jaeger on :16686
make setup                    # uv sync, npm ci, gitleaks pre-commit hook
make bench                    # the 360-run matrix above
make test                     # ruff, mypy, pytest, eslint, design lint, tsc, vitest
make e2e                      # Playwright against a real benchmark; refreshes screenshots
```

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), Node 22 and Docker.

## Limitations

- **One workflow, three faults, mock services.** The numbers describe this
  workflow under these faults. They are not rates for any real system.
- **The deterministic agent is a scripted naive-retry policy,** not an LLM. It
  models one common behaviour. Real models may verify, give up or improvise.
- **Variation across seeds comes from which step the fault hits** and the order
  contents. Within one (fault, step, controls) combination the outcome is fixed.
- **Latency in deterministic mode is logical time** (20 ms per call, 1000 ms
  per timeout), not measured latency.
- **Verify-after-write depends on a read endpoint that reflects the write.** A
  service with a lagging read path would defeat it.
- **Trace replay adjusts recorded decisions** when controls differ from the
  recording: ids come from the current run, and a recorded retry is skipped if
  the call it retried succeeded this time. Each skip is logged as an event.
- **The provider pool has run against one real provider (Groq) only.**
  Failover and circuit breaking between providers are tested with fakes.
- **Failover happens between runs, not inside one.** If the chosen provider
  fails mid-run, that run ends with an LLM error rather than switching model.
- **CI has not completed a run on GitHub.** The first run ended in
  `startup_failure` before any job was created; see the Actions tab.
- **Live results are one model, n=3 per scenario.** They show the failure modes
  happen with a real model; they are not rates.

Design choices are logged in [DECISIONS.md](DECISIONS.md); the UI review is in
[DESIGN_REVIEW.md](DESIGN_REVIEW.md).
