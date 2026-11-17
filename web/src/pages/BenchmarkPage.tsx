import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { PageHeader } from "../components/Layout";
import { OUTCOME_STYLE, OutcomeBadge } from "../components/OutcomeBadge";
import { Code, EmptyState, ErrorState, SkeletonBlock } from "../components/States";
import { api, type Cell, type Outcome } from "../lib/api";
import { fixed, OUTCOME_LABEL, OUTCOME_ORDER, signed } from "../lib/format";

const FILL: Record<string, string> = {
  verified_recovery: "var(--verified)",
  partial_recovery: "var(--partial)",
  manual_intervention_required: "var(--manual)",
  undetected_violation: "var(--violation)",
  indeterminate: "var(--indeterminate)",
};

function runsLink(batch: string, cell: Cell) {
  return `/runs?batch=${batch}&fault=${cell.fault_type}&controls=${encodeURIComponent(cell.controls)}`;
}

function MatrixCell({ batch, cell }: { batch: string; cell: Cell }) {
  return (
    <td className="border-l border-border p-0 align-top">
      <Link to={runsLink(batch, cell)} className="block px-2 py-2 transition-colors hover:bg-raised"
        title={`Open the ${cell.runs} runs behind this cell`}>
        <div className="num text-base">
          {cell.outcomes.verified_recovery}/{cell.runs}
          <span className="ml-1 text-xs text-muted">verified ({fixed(cell.verified_pct)}%)</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
          {OUTCOME_ORDER.filter((name) => name !== "verified_recovery" && cell.outcomes[name] > 0).map((name) => (
            <span key={name} className={`inline-flex items-center gap-1 text-xs ${OUTCOME_STYLE[name as Outcome].text}`}>
              {OUTCOME_STYLE[name as Outcome].icon}
              <span className="num">{cell.outcomes[name]}</span> {OUTCOME_LABEL[name].replace(/_/g, " ")}
            </span>
          ))}
        </div>
      </Link>
    </td>
  );
}

function OutcomeChart({ fault, cells }: { fault: string; cells: Cell[] }) {
  const data = cells.map((cell) => ({ controls: cell.controls.replace(/\+/g, " + "), ...cell.outcomes }));
  return (
    <figure className="rounded border border-border p-3">
      <figcaption className="text-sm font-medium">
        How do {cells[0]?.runs ?? 0} runs of <span className="font-mono">{fault}</span> end under each controls config?
      </figcaption>
      <div className="mt-2 h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 16, left: 8 }} barCategoryGap={6}>
            <CartesianGrid horizontal={false} stroke="var(--border)" />
            <XAxis type="number" allowDecimals={false} stroke="var(--muted)" tick={{ fontSize: 12, fill: "var(--muted)" }}
              label={{ value: "runs", position: "insideBottom", offset: -8, fontSize: 12, fill: "var(--muted)" }} />
            <YAxis type="category" dataKey="controls" width={200} stroke="var(--muted)"
              tick={{ fontSize: 12, fill: "var(--text)", fontFamily: "IBM Plex Mono" }} />
            <Tooltip cursor={{ fill: "var(--raised)" }} isAnimationActive={false}
              contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 4, fontSize: 12 }}
              labelStyle={{ color: "var(--text)" }} itemStyle={{ color: "var(--text)" }} />
            {OUTCOME_ORDER.map((name) => (
              <Bar key={name} dataKey={name} name={OUTCOME_LABEL[name]} stackId="outcome" fill={FILL[name]} isAnimationActive={false} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function BenchmarkPage() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const requested = params.get("batch") ?? undefined;
  const [seeds, setSeeds] = useState(20);
  const bench = useQuery({ queryKey: ["benchmark", requested ?? "latest"], queryFn: () => api.benchmark(requested) });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.batches });
  const start = useMutation({
    mutationFn: () => api.startBenchmark(seeds),
    onSuccess: async ({ batch_id }) => {
      await client.invalidateQueries();
      setParams({ batch: batch_id });
    },
  });

  const header = (
    <PageHeader title="Benchmark: fault type × controls config">
      <label className="text-xs text-muted" htmlFor="bench-batch">Batch</label>
      <select id="bench-batch" value={bench.data?.batch_id ?? ""} onChange={(e) => setParams({ batch: e.target.value })}
        className="h-7 rounded border border-border bg-surface px-2 font-mono text-xs">
        {batches.data?.filter((b) => b.batch_id !== "adhoc").map((b) => (
          <option key={b.batch_id} value={b.batch_id}>{b.batch_id} ({b.runs} runs)</option>
        ))}
      </select>
      <label className="ml-4 text-xs text-muted" htmlFor="seeds">Seeds per cell</label>
      <input id="seeds" type="number" min={1} max={200} value={seeds} onChange={(e) => setSeeds(Number(e.target.value))}
        className="num h-7 w-16 rounded border border-border bg-surface px-2 text-xs" />
      <button type="button" onClick={() => start.mutate()} disabled={start.isPending}
        className="h-7 rounded bg-accent px-3 text-xs font-medium text-accent-text transition-opacity disabled:opacity-50">
        {start.isPending ? "Running matrix…" : "Run matrix"}
      </button>
    </PageHeader>
  );

  if (bench.isPending) return <div className="flex h-full flex-col">{header}<SkeletonBlock lines={12} /></div>;
  if (bench.isError) return <div className="flex h-full flex-col">{header}<ErrorState error={bench.error} retry={() => bench.refetch()} /></div>;
  const { batch_id, cells, deltas } = bench.data;
  if (!batch_id || cells.length === 0) {
    return (
      <div className="flex h-full flex-col">
        {header}
        {start.isError && <ErrorState error={start.error} retry={() => start.reset()} />}
        <EmptyState title="No benchmark batch stored yet">
          Press Run matrix, or run <Code>make bench</Code>, to execute 3 faults × 6 controls configs × N seeds.
        </EmptyState>
      </div>
    );
  }
  const faults = [...new Set(cells.map((c) => c.fault_type))];
  const configs = [...new Set(cells.map((c) => c.controls))];
  const at = (fault: string, controls: string) => cells.find((c) => c.fault_type === fault && c.controls === controls);
  const meta = batches.data?.find((b) => b.batch_id === batch_id);

  return (
    <div className="flex h-full flex-col">
      {header}
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        {start.isError && <ErrorState error={start.error} retry={() => start.reset()} />}
        <p className="text-xs text-muted" data-testid="batch-summary">
          Batch <span className="font-mono text-text">{batch_id}</span> ·{" "}
          <span className="num text-text">{cells.reduce((n, c) => n + c.runs, 0)}</span> runs
          {meta && <> · seeds <span className="num text-text">{meta.first_seed}–{meta.last_seed}</span> · mode <span className="font-mono text-text">{meta.modes}</span></>}
          {" "}· every number below is computed from these stored runs.
        </p>

        <section>
          <h2 className="mb-2 text-base font-semibold">Verified-correct orders by fault type and controls config</h2>
          <table className="w-full table-fixed border border-border text-left">
            <thead className="bg-surface text-xs">
              <tr className="h-row border-b border-border">
                <th className="w-72 px-2">Controls config</th>
                {faults.map((fault) => <th key={fault} className="border-l border-border px-2 font-mono">{fault}</th>)}
              </tr>
            </thead>
            <tbody>
              {configs.map((config) => (
                <tr key={config} className="border-b border-border">
                  <th className="px-2 py-2 align-top font-mono text-xs font-medium">{config.replace(/\+/g, " + ")}</th>
                  {faults.map((fault) => {
                    const cell = at(fault, config);
                    return cell ? <MatrixCell key={fault} batch={batch_id} cell={cell} /> : <td key={fault} />;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold">Controls off against all controls on (change in run counts)</h2>
          <table className="w-full max-w-4xl border border-border text-left">
            <thead className="bg-surface text-xs">
              <tr className="h-row border-b border-border">
                <th className="px-2">Fault</th>
                <th className="px-2 text-right">Verified runs</th>
                <th className="px-2 text-right">Undetected-violation runs</th>
                <th className="px-2 text-right">Invariant violations</th>
                <th className="px-2 text-right">Duplicate operations committed</th>
              </tr>
            </thead>
            <tbody>
              {deltas.map((delta) => (
                <tr key={delta.fault_type} className="h-row border-b border-border">
                  <td className="px-2 font-mono text-xs">{delta.fault_type}</td>
                  <td className="num px-2 text-right">{signed(delta.verified)}</td>
                  <td className="num px-2 text-right">{signed(delta.undetected_violation)}</td>
                  <td className="num px-2 text-right">{signed(delta.invariant_violations)}</td>
                  <td className="num px-2 text-right">{signed(delta.duplicate_committed_operations)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section>
          <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" aria-label="Chart legend">
            <span className="text-muted">Bar segments, left to right:</span>
            {OUTCOME_ORDER.map((name) => (
              <span key={name} className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-dot" style={{ background: FILL[name] }} />
                <OutcomeBadge outcome={name as Outcome} />
              </span>
            ))}
          </div>
          <div className="grid grid-cols-3 gap-4">
          {faults.map((fault) => <OutcomeChart key={fault} fault={fault} cells={cells.filter((c) => c.fault_type === fault)} />)}
          </div>
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold">Duplicate requests, duplicate committed operations and violations per cell</h2>
          <table className="w-full border border-border text-left">
            <thead className="sticky top-0 bg-surface text-xs">
              <tr className="h-row border-b border-border">
                <th className="px-2">Fault</th>
                <th className="px-2">Controls</th>
                <th className="px-2 text-right">Runs</th>
                <th className="px-2 text-right">Duplicate requests</th>
                <th className="px-2 text-right">Duplicate ops committed</th>
                <th className="px-2 text-right">Invariant violations</th>
                <th className="px-2 text-right">Violations per run, mean</th>
                <th className="px-2 text-right">Variance</th>
                <th className="px-2 text-right">Latency p50 (logical ms)</th>
                <th className="px-2 text-right">p95</th>
              </tr>
            </thead>
            <tbody>
              {cells.map((cell) => (
                <tr key={`${cell.fault_type}/${cell.controls}`} className="h-row border-b border-border">
                  <td className="px-2 font-mono text-xs">{cell.fault_type}</td>
                  <td className="px-2 font-mono text-xs">
                    <Link className="text-accent hover:underline" to={runsLink(batch_id, cell)}>{cell.controls}</Link>
                  </td>
                  <td className="num px-2 text-right">{cell.runs}</td>
                  <td className="num px-2 text-right">{cell.duplicate_requests}</td>
                  <td className="num px-2 text-right">{cell.duplicate_committed_operations}</td>
                  <td className="num px-2 text-right">{cell.invariant_violations}</td>
                  <td className="num px-2 text-right">{fixed(cell.violations_per_run_mean, 2)}</td>
                  <td className="num px-2 text-right">{fixed(cell.violations_per_run_variance, 2)}</td>
                  <td className="num px-2 text-right">{fixed(cell.latency_p50_ms)}</td>
                  <td className="num px-2 text-right">{fixed(cell.latency_p95_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
