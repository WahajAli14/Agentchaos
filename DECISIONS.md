# Decisions

Choices made where the spec was ambiguous. Simplest option taken each time.

## Day 1 (2026-11-12)

| # | Decision | Why |
|---|---|---|
| D1 | Repo root is the `agentchaos/` folder itself (the original working folder, renamed). Git initialised locally; nothing committed or pushed. Intended remote: the existing private GitHub repo `WahajAli14/Agentchaos` (capital A on GitHub; folder and package stay lowercase). | The owner asked for the working folder to be the project base. GitHub repo names are case-insensitive in URLs, so no rename is needed. Commits and pushes are left to the owner. |
| D2 | Logical time is an integer count of milliseconds from run start (`ManualClock.now_ms()`), not a datetime. | One shared time axis for the timeline, no timezone handling, and a wall-clock leak is obvious (epoch-sized value). |
| D3 | Deterministic mode runs all four services in one process over `httpx.ASGITransport`, sharing one clock, scheduler and ID generator (`ServiceCluster`). Docker Compose runs the same apps as four processes, each with its own logical clock driven through `/_admin/clock/advance`. | In-process is the only way to get a single controlled clock and no uncontrolled concurrency. Benchmarks will use the in-process cluster. |
| D4 | Route handlers are `async def` and do synchronous SQLite work inline. | Keeps every request on one thread in arrival order. Using FastAPI's threadpool would add scheduling the tests cannot control. |
| D5 | SQLAlchemy 2 Core tables (not ORM classes), one `MetaData` per service. | Each service owns a separate database; Core makes "dump every table" snapshots trivial. |
| D6 | Seeded IDs are `<prefix>_<4-digit counter><6 hex from seeded RNG>`. | Counter guarantees uniqueness, RNG makes IDs depend on the seed. |
| D7 | Flaws are per-service settings, defaulting to the flawed behaviour: `idempotency_supported=False`, `accept_duplicates=True`, `async_commit_delay_ms=0`. | The baseline should be the realistic unsafe integration; controls and toggles improve on it. |
| D8 | Async flaw: a deferrable mutation returns `202 {"status":"processing","id":...}` and commits after the delay via the scheduler. Nothing is validated before the 202, so an accepted request can still fail to commit. `POST /orders` is never deferred. | Models "200/202 before commit". Later steps need the order row to exist. |
| D9 | With server idempotency on, the first response (including a rejection or a 202) is stored and replayed; the same key with a different body gets 422. | Common provider behaviour; keeps replay semantics simple. |
| D10 | Each service keeps a `request_log` of every mutation that reached it, with a phase (`committed`, `rejected`, `replayed`, `accepted`, `deferred_committed`, `deferred_rejected`, `idempotency_conflict`). | Needed to report duplicate requests separately from duplicate committed operations. |
| D11 | "Exactly one invoice per confirmed order" is split into `single_invoice_per_confirmed_order` (more than one) and `no_confirmed_order_without_invoice` (none). | Otherwise a missing invoice is counted under two invariants. |
| D12 | Only `issued` invoices and `active` reservations count in invariants. | Voids and releases are compensations: the original row stays as history, but it is not current state. |
| D13 | `reserved_stock_matches_confirmed_orders` compares three numbers per SKU: `stock.reserved`, total active reservation quantity, total confirmed order quantity. It has no grace period. | It is checked after a run ends; an unconfirmed order with a live reservation is already wrong at that point. |
| D14 | `no_orphaned_reservations`: active reservation whose order is not confirmed (or does not exist) and is older than a grace period, default 30 000 logical ms, configurable on the workflow. | Spec says "after N seconds" without N. |
| D15 | Results DB is append-only at the database level: SQLite triggers abort every UPDATE and DELETE. Run state is a log of transition rows; current state is the latest row. | "Never erase or reset results" is enforced by the database, not by convention. |
| D16 | `ResultsStore.record_outcome` refuses: any classification for an `indeterminate` run, any outcome for a run still `running`/`pending_reconciliation`, any classification without stored invariant results, and `verified_recovery` when a stored invariant failed. | Last line of defence for non-negotiable 2, independent of the Day 3 classifier. |
| D17 | Results rows carry a wall-clock `recorded_at` set by SQLite. | Historical records need a real date. It is excluded from canonical records, and it is the only allowed wall-clock use (enforced by a source scan test). |
| D18 | `AGENTCHAOS_DATA_DIR` (default `data/`) is read from the environment but is not a `.env` key. | `.env` must contain exactly the specified keys; Compose and tests still need to place the databases. |
| D19 | `FaultPolicy` and `SafetyControl` exist as protocols only; their shapes are provisional. | Their implementations are Day 2 and Day 3. |
| D20 | No Postman collections, OpenAPI files or other API tooling added. | Scope is frozen. |
| D21 | `.env` is created with the same blank keys as `.env.example`. | "Empty `.env` with exactly these keys." |

## Day 2 and Day 3 (2026-11-17)

| # | Decision | Why |
|---|---|---|
| D22 | The fault target step is chosen by the run's seeded RNG from the steps eligible for that fault; the order's SKU and quantity also come from the seed. | This is what varies across seeds in deterministic mode. |
| D23 | `partial_commit` makes the target step's service unavailable for every attempt. | A one-off failure is fixed by a naive retry and demonstrates nothing. |
| D24 | Deterministic mode uses a scripted naive-retry agent only. Replay of recorded decision traces is deferred to P1, where live runs produce traces. | The spec allows either; the scripted policy is enough for P0. |
| D25 | Classification: violated + verified escalation = `manual_intervention_required`; violated otherwise = `undetected_violation`; clean, order confirmed, zero duplicate committed operations = `verified_recovery`; clean with a verified escalation = `manual_intervention_required`; clean otherwise = `partial_recovery`. | A stray duplicate that breaks no invariant (second order, second notification) must not be reported as a clean recovery. |
| D26 | An escalation only counts if a control produced it after querying actual state. An agent that simply gives up has not escalated. | "Verified escalation decision." |
| D27 | Duplicate requests and duplicate committed operations are both computed from the services' own request logs, grouped by request hash. A no-op (confirming a confirmed order) is logged as `noop`, not `committed`. | Keeps the two numbers separate and grounded in what reached the services. |
| D28 | Benchmark has a sixth config, `idempotency_client_only` (keys sent, services ignore them). | Evidence for the server-cooperation requirement; it scores the same as no controls. |
| D29 | Run latency in deterministic mode is logical milliseconds until the agent and controls finished (20 ms per call, 1000 ms per timeout). | The engine may not read wall-clock time. Real latency arrives with live mode. |
| D30 | After the agent finishes, the engine advances logical time past the orphan grace period before checking invariants. | Makes "orphaned" decidable in every run. |
| D31 | The minimal run-detail page is static HTML served by the results API; the React app replaces it in P1. | Day 2 asks for a plain timeline from real data, no polish. |
| D32 | `Makefile` exports `PYTHONPATH=src`. | macOS marks the editable-install `.pth` hidden and Python 3.12 skips it. |

## P1 and P2 (2026-11-17)

| # | Decision | Why |
|---|---|---|
| D33 | P2 was started while one P1 item is open: live smoke runs, which need a provider key in `.env`. | The owner asked for everything to be implemented; the open item is blocked on a key only the owner can add. |
| D34 | Live agent tools are the five workflow calls plus three reads and three undo calls. Each call goes through the same controls and proxy as the scripted agent. | A realistic assistant can look things up and undo; the controls must see the same traffic either way. |
| D35 | The live agent states its belief with a final `RESULT: SUCCESS` or `RESULT: FAILED` line; anything else is recorded as `unknown`. | Needed to compare what the agent reports with database state. |
| D36 | Trace replay re-issues recorded tool calls. Id arguments come from the current run, and a recorded retry is skipped when the call it retried (ambiguous in the recording) succeeds this time. Skips are logged as events. | A retry was a decision conditional on a failure; replaying it unconditionally under a control would create a duplicate the agent never chose. |
| D37 | Live-mode latency is measured wall time of the agent loop; the live agent and provider client are the only modules allowed to read real time (enforced by the source scan). | The model call is real; everything it calls still runs on the logical clock. |
| D38 | Estimated cost for runs with no LLM call is 0, not unknown. | Nothing was used, so there is nothing to estimate. |
| D39 | The pool pins one provider per run. A mid-run provider failure ends that run with an LLM error; failover happens at the next selection. | Spec: never mix models in one statistic. |
| D40 | Health check = one tool-calling probe per provider. It does not separately list models. | A successful tool call proves the model id exists and supports tools; one request instead of two against free-tier quotas. |
| D41 | The API runs scenarios on its own copy of the mock services (`data/api-services`), not the standalone service containers. | Runs need the shared logical clock, which only the in-process cluster has. |
| D42 | The React app is served by the API process; the two static HTML pages remain as a fallback when the frontend is not built. | One container, no separate web server. |
| D43 | The Tailwind theme is replaced, not extended. | Banned styles (default palettes, large radii, shadows) then do not exist as classes. |
| D44 | Benchmark "variance" is the population variance of invariant violations per run within a cell. | The spec asks for variance without defining it; outcome classes are categorical. |
| D45 | Tracing is enabled only by the API process and only if a collector answers at the configured endpoint. | `.env.example` always sets an endpoint; without the check every local run would log export errors. |
| D46 | Live runs ended by the provider (an `llm_error` event) are kept in the results database but excluded from per-model statistics, and counted separately. | A rate-limited run reflects provider quota, not agent behaviour; results stay append-only. |
| D47 | On 429/5xx the client waits max(own exponential backoff, the provider's `Retry-After` or rate-limit reset header), capped at 60 s, up to 6 attempts. | The first Groq batch lost 6 of 18 runs to a 7-second total backoff. |

