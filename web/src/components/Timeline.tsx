import type { RunDetail, TimelineEvent } from "../lib/api";
import { buildRows } from "../lib/timeline";
import { WarnIcon } from "./icons";

const LANES: { id: TimelineEvent["lane"]; title: string; sub: string }[] = [
  { id: "agent", title: "Agent", sub: "decisions and tool calls" },
  { id: "proxy", title: "Proxy and API", sub: "injected faults, real responses" },
  { id: "ground_truth", title: "Ground truth", sub: "service databases" },
];
const GRID = "grid grid-cols-timeline";

function tone(event: TimelineEvent): string {
  if (event.kind === "fault_injected" || event.kind === "escalation") return "border-l-manual";
  if (event.kind === "invariants_checked") return event.payload.passed ? "border-l-verified" : "border-l-violation";
  if (event.kind.startsWith("control_")) return "border-l-accent";
  if (event.kind === "observation" && event.payload.timed_out) return "border-l-partial";
  return "border-l-border-strong";
}

export function Timeline({ detail }: { detail: RunDetail }) {
  const rows = buildRows(detail);
  return (
    <section aria-label="Run timeline" className="rounded border border-border">
      <div className={`${GRID} sticky top-0 z-10 border-b border-border bg-surface text-xs`}>
        <div className="px-2 py-2 text-right text-muted">logical ms</div>
        {LANES.map((lane) => (
          <div key={lane.id} className="border-l border-border px-2 py-2">
            <span className="font-semibold">{lane.title}</span> <span className="text-muted">{lane.sub}</span>
          </div>
        ))}
      </div>
      {rows.map(({ event, described, showTime, gapMs, mismatch }) => (
        <div key={event.seq}>
          {gapMs >= 500 && (
            <div className={`${GRID} border-b border-border bg-surface text-xs text-muted`}>
              <div className="num px-2 py-1 text-right">+{gapMs}</div>
              <div className="col-span-3 border-l border-border px-2 py-1">{gapMs} ms pass with no activity</div>
            </div>
          )}
          <div className={`${GRID} border-b border-border ${mismatch ? "bg-mismatch" : ""}`} data-kind={event.kind}>
            <div className="num px-2 py-1 text-right text-xs text-muted">{showTime ? event.logical_ms : ""}</div>
            {LANES.map((lane) => (
              <div key={lane.id} className="min-w-0 border-l border-border px-2 py-1">
                {lane.id === event.lane && (
                  <div className={`border-l-2 pl-2 ${tone(event)}`}>
                    <div className="text-sm font-medium">{described.title}</div>
                    <div className="break-words font-mono text-xs text-muted">{described.detail}</div>
                  </div>
                )}
              </div>
            ))}
          </div>
          {mismatch && (
            <div className={`${GRID} border-b border-border bg-mismatch`} data-testid="mismatch">
              <div />
              <div className="col-span-3 flex items-start gap-2 border-l border-border px-2 py-2 text-manual">
                <span className="mt-0.5"><WarnIcon /></span>
                <div className="text-xs">
                  <div className="font-semibold">Belief and reality diverge here</div>
                  <div className="text-text">{mismatch.explanation}</div>
                  <div className="mt-1 font-mono text-text">
                    agent was told: {mismatch.told} · databases:{" "}
                    {mismatch.actual.length ? mismatch.actual.join(", ") : "no change"}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      ))}
    </section>
  );
}
