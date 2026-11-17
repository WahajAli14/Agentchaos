export type Classification =
  | "verified_recovery"
  | "partial_recovery"
  | "manual_intervention_required"
  | "undetected_violation";
export type Outcome = Classification | "indeterminate" | "unfinished";

export interface Run {
  run_id: string;
  batch_id: string | null;
  scenario: string;
  mode: string;
  seed: number;
  fault_type: string | null;
  controls: string[];
  provider: string | null;
  model: string | null;
  recorded_at: string;
  classification: Classification | null;
  final_state: string | null;
  explanation: string | null;
  agent_believed_outcome: string | null;
  goal_achieved: boolean | null;
  tool_calls: number | null;
  llm_calls: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  duplicate_requests: number | null;
  duplicate_committed_operations: number | null;
  billed_cost_usd: number | null;
  estimated_cost_usd: number | null;
  latency_ms: number | null;
}

export interface TimelineEvent {
  seq: number;
  lane: "agent" | "proxy" | "ground_truth";
  kind: string;
  logical_ms: number;
  // Event payloads are free-form JSON recorded by the engine.
  payload: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export interface Violation {
  invariant: string;
  entity_type: string;
  entity_id: string;
  detail: Record<string, unknown>;
}

export interface RunDetail {
  run: Run;
  states: string[];
  events: TimelineEvent[];
  mismatches: { seq: number; logical_ms: number; step: string; fault_type: string; explanation: string }[];
  reconciliation: { attempt: number; logical_ms: number; pending_operations: number; result: string }[];
  invariants: { invariant: string; passed: boolean; violations: Violation[] }[];
  outcome: Run | null;
}

export interface Cell {
  fault_type: string;
  controls: string;
  runs: number;
  outcomes: Record<Exclude<Outcome, "unfinished">, number>;
  verified_pct: number | null;
  violations_by_type: Record<string, number>;
  invariant_violations: number;
  duplicate_requests: number;
  duplicate_committed_operations: number;
  tool_calls: number;
  violations_per_run_mean: number | null;
  violations_per_run_variance: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  run_ids: string[];
}

export interface Delta {
  fault_type: string;
  off: string;
  on: string;
  verified: number;
  undetected_violation: number;
  invariant_violations: number;
  duplicate_committed_operations: number;
}

export interface Benchmark {
  batch_id: string | null;
  cells: Cell[];
  deltas: Delta[];
}

export interface Batch {
  batch_id: string;
  runs: number;
  first_seed: number;
  last_seed: number;
  recorded_at: string;
  modes: string;
  models: string | null;
}

export interface Scenarios {
  default_seed: number;
  can_run: boolean;
  faults: { fault_type: string; description: string; eligible_steps: string[] }[];
  controls: { name: string; description: string }[];
}

export interface InvariantBreakdown {
  invariant: string;
  description: string;
  runs_checked: number;
  runs_violated: number;
  violations: number;
  by_cell: { fault_type: string; controls: string; runs_violated: number; violations: number }[];
  examples: (Violation & { run_id: string })[];
}

export interface ProviderStatus {
  name: string;
  model: string | null;
  priority: number | null;
  enabled: boolean;
  healthy: boolean;
  health_checked: boolean;
  reason: string | null;
  masked_key: string;
  supports_tools: boolean | null;
  circuit: string | null;
  rate_limit: { used_last_minute: number; rpm: number | null; used_last_day: number; rpd: number | null } | null;
  calls: number;
  failures: number;
}

export interface LiveModel {
  provider: string;
  model: string;
  runs: number;
  outcomes: Record<string, number>;
  verified_runs: number;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  billed_cost_usd: number;
  estimated_cost_usd: number | null;
  tokens_per_verified_run: number | null;
  llm_calls_per_verified_run: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  note: string;
}

export interface Providers {
  providers: ProviderStatus[];
  failovers: { skipped: string; reason: string; chosen: string | null }[];
  live_models: LiveModel[];
}

export interface AppSettings {
  env: string;
  default_seed: number;
  live_mode_enabled: boolean;
  data_dir: string;
  results_db: string;
  otel_endpoint: string | null;
  runs_stored: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, `Cannot reach the API at ${path}. Is the server running?`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep status text */
    }
    throw new ApiError(response.status, `HTTP ${response.status} from ${path}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  scenarios: () => request<Scenarios>("/api/scenarios"),
  runs: (batch?: string) => request<Run[]>(`/api/runs${batch ? `?batch_id=${encodeURIComponent(batch)}` : ""}`),
  run: (id: string) => request<RunDetail>(`/api/runs/${encodeURIComponent(id)}`),
  batches: () => request<Batch[]>("/api/batches"),
  benchmark: (batch?: string) =>
    request<Benchmark>(`/api/benchmark${batch ? `?batch_id=${encodeURIComponent(batch)}` : ""}`),
  invariants: (batch?: string) =>
    request<{ batch_id: string | null; invariants: InvariantBreakdown[] }>(
      `/api/invariants${batch ? `?batch_id=${encodeURIComponent(batch)}` : ""}`,
    ),
  providers: () => request<Providers>("/api/providers"),
  checkProviders: () => request<{ providers: ProviderStatus[] }>("/api/providers/check", { method: "POST" }),
  settings: () => request<AppSettings>("/api/settings"),
  startRun: (body: { fault_type: string | null; controls: string[]; seed: number; target_step: string | null }) =>
    request<{ run_id: string }>("/api/runs", { method: "POST", body: JSON.stringify(body) }),
  startBenchmark: (seeds: number) =>
    request<{ batch_id: string }>("/api/benchmark", { method: "POST", body: JSON.stringify({ seeds }) }),
};

export function outcomeOf(run: Pick<Run, "classification" | "final_state">): Outcome {
  if (run.classification) return run.classification;
  return run.final_state === "indeterminate" ? "indeterminate" : "unfinished";
}

export function controlsLabel(controls: string[]): string {
  return controls.length ? [...controls].sort().join("+") : "none";
}
