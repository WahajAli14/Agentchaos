import type { RunDetail, TimelineEvent } from "./api";

export interface Described {
  title: string;
  detail: string;
}

const json = (value: unknown) => JSON.stringify(value);

export function describe(event: TimelineEvent): Described {
  const p = event.payload;
  switch (event.kind) {
    case "run_started":
      return {
        title: "Run started",
        detail: `order ${p.workflow_input.quantity} × ${p.workflow_input.sku} at ${p.workflow_input.unit_price_cents} cents for ${p.workflow_input.customer_id} · controls ${p.controls.length ? p.controls.join(", ") : "none"} · fault ${p.fault_type ?? "none"}${p.fault_target_step ? ` on ${p.fault_target_step}` : ""}`,
      };
    case "llm_call":
      return {
        title: `LLM turn ${p.turn}`,
        detail: `${p.provider}/${p.model} · ${p.prompt_tokens} in, ${p.completion_tokens} out · ${p.tool_calls.length ? `calls ${p.tool_calls.join(", ")}` : "no tool call"}`,
      };
    case "llm_error":
      return { title: "LLM call failed", detail: String(p.error) };
    case "message":
      return { title: "Agent message", detail: String(p.text) };
    case "tool_call":
      return { title: `Call ${p.tool ?? p.step}`, detail: `attempt ${p.attempt} · ${p.method} ${p.service}${p.path}` };
    case "observation":
      return {
        title: p.timed_out ? `Sees TIMEOUT for ${p.step}` : `Sees ${p.status} for ${p.step}`,
        detail: `attempt ${p.attempt}${p.source !== "service" ? ` · answered from ${p.source}` : ""}`,
      };
    case "decision":
      return { title: `Decides: ${String(p.action).replace("_", " ")}`, detail: `${p.step} · ${p.reason}` };
    case "finished":
      return {
        title: `Agent reports ${p.believed_outcome}`,
        detail: p.failed_step ? `gave up at ${p.failed_step}` : "no failed step",
      };
    case "replay_skipped":
      return { title: `Replay skips ${p.tool}`, detail: String(p.reason) };
    case "control_verify":
      return {
        title: `Control checks state: ${p.step}`,
        detail: p.committed ? `write is committed (${p.record_id})` : "write is not committed",
      };
    case "control_compensation":
      return {
        title: `Control compensates ${p.step}`,
        detail: p.skipped ? "no compensation defined" : `status ${p.status} · verified undone: ${p.verified_undone}`,
      };
    case "control_dedupe_hit":
      return { title: `Control answers ${p.step} from dedupe store`, detail: `key ${p.key}` };
    case "escalation":
      return { title: "Control escalates to a person", detail: `${p.message} Still live: ${json(p.leftover)}` };
    case "fault_injected":
      return { title: `Fault: ${p.fault_type}`, detail: `${p.step}, attempt ${p.attempt}` };
    case "delivered":
      return {
        title: `Service answers ${p.status}`,
        detail: `${p.method} ${p.service}${p.path}${p.idempotent_replay ? " · idempotent replay" : ""}${p.purpose !== "step" ? ` · ${p.purpose}` : ""}`,
      };
    case "response_to_agent":
      return {
        title: p.timed_out ? "Agent is told: TIMEOUT" : `Agent is told: ${p.status}`,
        detail: `${p.step}, attempt ${p.attempt}`,
      };
    case "state_change":
      return p.change === "inserted"
        ? { title: `${p.service}.${p.table} row inserted`, detail: `${p.id} ${summariseRow(p.row)}` }
        : {
            title: `${p.service}.${p.table} row updated`,
            detail: `${p.id} · ${Object.entries(p.fields as Record<string, [unknown, unknown]>)
              .map(([key, [before, after]]) => `${key}: ${before} → ${after}`)
              .join(", ")}`,
          };
    case "invariants_checked":
      return {
        title: p.passed ? "Invariants hold" : "Invariants violated",
        detail: Object.entries(p.violation_counts as Record<string, number>)
          .filter(([, n]) => n > 0)
          .map(([name, n]) => `${name} ×${n}`)
          .join(", ") || "0 violations",
      };
    default:
      return { title: event.kind, detail: json(p) };
  }
}

function summariseRow(row: Record<string, unknown>): string {
  const keep = ["order_id", "sku", "quantity", "amount_cents", "status", "reserved", "kind"];
  return keep
    .filter((key) => row[key] !== undefined && row[key] !== null)
    .map((key) => `${key}=${row[key]}`)
    .join(" ");
}

export interface Mismatch {
  explanation: string;
  told: string;
  actual: string[];
}

export interface Row {
  event: TimelineEvent;
  described: Described;
  showTime: boolean;
  gapMs: number;
  mismatch: Mismatch | null;
}

/** Events in order, with the shared time axis and belief-versus-truth markers. */
export function buildRows(detail: Pick<RunDetail, "events" | "mismatches">): Row[] {
  const explanations = new Map(detail.mismatches.map((m) => [m.seq, m.explanation]));
  let previous = -1;
  return detail.events.map((event, index) => {
    const showTime = event.logical_ms !== previous;
    const gapMs = previous >= 0 ? event.logical_ms - previous : 0;
    previous = event.logical_ms;
    let mismatch: Mismatch | null = null;
    const explanation = explanations.get(event.seq);
    if (explanation !== undefined) {
      const rest = detail.events.slice(index + 1);
      const end = rest.findIndex((e) => e.kind === "response_to_agent" && e.payload.step === event.payload.step);
      const window = end === -1 ? rest : rest.slice(0, end + 1);
      const told = window.find((e) => e.kind === "response_to_agent");
      mismatch = {
        explanation,
        told: told ? (told.payload.timed_out ? "TIMEOUT" : `status ${told.payload.status}`) : "nothing",
        actual: window
          .filter((e) => e.kind === "state_change")
          .map((e) => `${e.payload.service}.${e.payload.table} ${e.payload.id} ${e.payload.change}`),
      };
    }
    return { event, described: describe(event), showTime, gapMs, mismatch };
  });
}
