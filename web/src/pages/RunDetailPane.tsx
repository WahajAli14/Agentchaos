import { useQuery } from "@tanstack/react-query";
import { OUTCOME_STYLE, OutcomeBadge } from "../components/OutcomeBadge";
import { ErrorState, SkeletonBlock } from "../components/States";
import { Timeline } from "../components/Timeline";
import { api, outcomeOf, type RunDetail } from "../lib/api";
import { fixed, usd } from "../lib/format";

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="border-l border-border px-3 first:border-l-0 first:pl-0">
      <div className="text-xs text-muted">{label}</div>
      <div className="num text-base">
        {value}
        {unit && <span className="ml-1 text-xs text-muted">{unit}</span>}
      </div>
    </div>
  );
}

function Verdict({ detail }: { detail: RunDetail }) {
  const outcome = detail.outcome;
  if (!outcome) return <p className="text-muted">This run has no stored outcome yet.</p>;
  const kind = outcomeOf(outcome);
  const confirmed = outcome.goal_achieved === null ? "unknown" : outcome.goal_achieved ? "yes" : "no";
  const believedSuccess = outcome.agent_believed_outcome === "success";
  const wrongBelief = believedSuccess && kind !== "verified_recovery";
  return (
    <div className={`rounded border border-l-4 border-border bg-surface p-3 ${OUTCOME_STYLE[kind].border}`}>
      <OutcomeBadge outcome={kind} />
      <p className="mt-1">{outcome.explanation}</p>
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">Agent reported</dt>
        <dd className="font-mono">{outcome.agent_believed_outcome ?? "nothing"}</dd>
        <dt className="text-muted">Order confirmed in the database</dt>
        <dd className="font-mono">{confirmed}</dd>
      </dl>
      {wrongBelief && (
        <p className="mt-2 text-xs text-manual">
          The agent reported success. The databases do not support a clean success.
        </p>
      )}
    </div>
  );
}

export function RunDetailPane({ runId }: { runId: string }) {
  const query = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId) });
  if (query.isPending) return <SkeletonBlock lines={14} />;
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const detail = query.data;
  const { run, outcome } = detail;
  const live = run.mode === "live";
  return (
    <div className="space-y-4 p-4" data-testid="run-detail">
      <div>
        <h2 className="text-lg font-semibold">
          <span className="font-mono">{run.run_id}</span> <span className="text-muted">{run.scenario}</span>
        </h2>
        <p className="mt-1 text-xs text-muted">
          mode <span className="font-mono text-text">{run.mode}</span> · seed{" "}
          <span className="num text-text">{run.seed}</span> · fault{" "}
          <span className="font-mono text-text">{run.fault_type ?? "none"}</span> · controls{" "}
          <span className="font-mono text-text">{run.controls.length ? run.controls.join(", ") : "none"}</span>
          {run.model && (
            <>
              {" "}· model <span className="font-mono text-text">{run.provider}/{run.model}</span>
            </>
          )}{" "}
          · execution state <span className="font-mono text-text">{detail.states.join(" → ")}</span>
        </p>
      </div>
      <Verdict detail={detail} />
      {outcome && (
        <div className="flex flex-wrap gap-y-2">
          <Stat label="Tool calls" value={fixed(outcome.tool_calls)} />
          <Stat label="Duplicate requests reaching a service" value={fixed(outcome.duplicate_requests)} />
          <Stat label="Duplicate operations committed" value={fixed(outcome.duplicate_committed_operations)} />
          <Stat label={live ? "Agent latency" : "Run latency"} value={fixed(outcome.latency_ms)} unit={live ? "ms wall" : "logical ms"} />
          {live && <Stat label="LLM calls" value={fixed(outcome.llm_calls)} />}
          {live && <Stat label="Tokens in / out" value={`${fixed(outcome.prompt_tokens)} / ${fixed(outcome.completion_tokens)}`} />}
          {live && <Stat label="Billed cost" value={usd(outcome.billed_cost_usd)} />}
          {live && <Stat label="Estimated cost at list price" value={usd(outcome.estimated_cost_usd)} />}
        </div>
      )}
      <div>
        <h3 className="mb-2 text-base font-semibold">What the agent believed against what happened</h3>
        <Timeline detail={detail} />
      </div>
      {detail.reconciliation.length > 0 && (
        <div>
          <h3 className="mb-2 text-base font-semibold">Reconciliation attempts</h3>
          <table className="w-full border border-border text-left">
            <thead className="bg-surface text-xs">
              <tr className="h-row border-b border-border">
                <th className="px-2 text-right">Attempt</th>
                <th className="px-2 text-right">Logical ms</th>
                <th className="px-2 text-right">Writes still uncommitted</th>
                <th className="px-2">Result</th>
              </tr>
            </thead>
            <tbody>
              {detail.reconciliation.map((attempt) => (
                <tr key={attempt.attempt} className="h-row border-b border-border">
                  <td className="num px-2 text-right">{attempt.attempt}</td>
                  <td className="num px-2 text-right">{attempt.logical_ms}</td>
                  <td className="num px-2 text-right">{attempt.pending_operations}</td>
                  <td className="px-2 font-mono text-xs">{attempt.result}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div>
        <h3 className="mb-2 text-base font-semibold">Invariants checked against the service databases</h3>
        <table className="w-full border border-border text-left">
          <thead className="bg-surface text-xs">
            <tr className="h-row border-b border-border">
              <th className="px-2">Invariant</th>
              <th className="px-2">Result</th>
              <th className="px-2">Violating records</th>
            </tr>
          </thead>
          <tbody>
            {detail.invariants.map((result) => (
              <tr key={result.invariant} className="border-b border-border align-top">
                <td className="px-2 py-2 font-mono text-xs">{result.invariant}</td>
                <td className={`px-2 py-2 text-xs ${result.passed ? "text-verified" : "text-violation"}`}>
                  {result.passed ? "holds" : `violated ×${result.violations.length}`}
                </td>
                <td className="px-2 py-2 font-mono text-xs text-muted">
                  {result.violations.map((v) => `${v.entity_type} ${v.entity_id} ${JSON.stringify(v.detail)}`).join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
