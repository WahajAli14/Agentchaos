import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "../components/Layout";
import { OUTCOME_STYLE, OutcomeBadge } from "../components/OutcomeBadge";
import { Code, EmptyState, ErrorState, SkeletonBlock } from "../components/States";
import { api, controlsLabel, outcomeOf, type Outcome } from "../lib/api";
import { fixed, OUTCOME_ORDER } from "../lib/format";
import { useKeys } from "../lib/useKeys";

const BASELINE = { fault_type: "no_fault", description: "No fault is injected. The workflow should end verified.", eligible_steps: [] as string[] };

export function ScenariosPage() {
  const { fault = "timeout_after_commit" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const scenarios = useQuery({ queryKey: ["scenarios"], queryFn: api.scenarios });
  const recent = useQuery({ queryKey: ["runs", "adhoc"], queryFn: () => api.runs("adhoc") });
  const bench = useQuery({ queryKey: ["benchmark", "latest"], queryFn: () => api.benchmark() });
  const [controls, setControls] = useState<string[]>([]);
  const [seed, setSeed] = useState<number | null>(null);
  const [step, setStep] = useState("");

  const start = useMutation({
    mutationFn: api.startRun,
    onSuccess: async ({ run_id }) => {
      await client.invalidateQueries({ queryKey: ["runs"] });
      await client.invalidateQueries({ queryKey: ["batches"] });
      navigate(`/runs/${run_id}?batch=adhoc`);
    },
  });

  const formRef = useRef<HTMLFormElement>(null);
  useKeys({
    r: () => formRef.current?.requestSubmit(),
    j: () => stepScenario(1),
    k: () => stepScenario(-1),
  });
  const names = [...(scenarios.data?.faults.map((f) => f.fault_type) ?? []), "no_fault"];
  const stepScenario = (step: number) => {
    const next = names[names.indexOf(fault) + step];
    if (next) navigate(`/scenarios/${next}`);
  };

  if (scenarios.isPending) return <SkeletonBlock lines={10} />;
  if (scenarios.isError) return <ErrorState error={scenarios.error} retry={() => scenarios.refetch()} />;
  const faults = [...scenarios.data.faults, BASELINE];
  const selected = faults.find((f) => f.fault_type === fault) ?? faults[0];
  const effectiveSeed = seed ?? scenarios.data.default_seed;

  const toggle = (name: string) => {
    setControls((current) => {
      if (current.includes(name)) return current.filter((c) => c !== name);
      const exclusive = name.startsWith("idempotency") ? current.filter((c) => !c.startsWith("idempotency")) : current;
      return [...exclusive, name];
    });
  };
  const evidence = (bench.data?.cells ?? []).filter((cell) => cell.fault_type === selected.fault_type);
  const mine = (recent.data ?? []).filter((run) => (run.fault_type ?? "no_fault") === selected.fault_type).slice(-8).reverse();

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Scenarios: inject one fault into the order workflow" />
      <div className="flex min-h-0 flex-1">
        <ul className="w-72 shrink-0 overflow-auto border-r border-border" aria-label="Fault scenarios">
          {faults.map((item) => (
            <li key={item.fault_type}>
              <Link to={`/scenarios/${item.fault_type}`} onClick={() => setStep("")}
                className={`block border-b border-l-2 border-border px-3 py-2 transition-colors hover:bg-raised ${
                  item.fault_type === selected.fault_type ? "border-l-accent bg-raised" : "border-l-transparent"
                }`}>
                <div className="font-mono text-sm">{item.fault_type}</div>
                <div className="text-xs text-muted">{item.description}</div>
              </Link>
            </li>
          ))}
        </ul>
        <div className="grid min-w-0 flex-1 grid-cols-2 gap-6 overflow-auto p-4">
          <div className="min-w-0">
          <h2 className="font-mono text-lg font-semibold">{selected.fault_type}</h2>
          <p className="mt-1 max-w-2xl text-muted">{selected.description}</p>
          <form ref={formRef} className="mt-4 max-w-2xl space-y-4" onSubmit={(e) => {
            e.preventDefault();
            start.mutate({
              fault_type: selected.fault_type === "no_fault" ? null : selected.fault_type,
              controls, seed: effectiveSeed, target_step: step || null,
            });
          }}>
            <fieldset className="rounded border border-border">
              <legend className="ml-2 px-1 text-xs font-semibold">Safety controls for this run</legend>
              {scenarios.data.controls.map((control) => (
                <label key={control.name} className="flex cursor-pointer items-start gap-2 border-b border-border px-3 py-2 last:border-b-0 hover:bg-raised">
                  <input type="checkbox" className="mt-1 accent-accent" checked={controls.includes(control.name)} onChange={() => toggle(control.name)} />
                  <span>
                    <span className="font-mono text-sm">{control.name}</span>
                    <span className="block text-xs text-muted">{control.description}</span>
                  </span>
                </label>
              ))}
            </fieldset>
            <div className="flex items-end gap-4">
              <label className="text-xs text-muted">
                Seed
                <input type="number" value={effectiveSeed} onChange={(e) => setSeed(Number(e.target.value))}
                  className="num mt-1 block h-8 w-28 rounded border border-border bg-surface px-2 text-sm text-text" />
              </label>
              {selected.eligible_steps.length > 0 && (
                <label className="text-xs text-muted">
                  Step the fault hits
                  <select value={step} onChange={(e) => setStep(e.target.value)}
                    className="mt-1 block h-8 rounded border border-border bg-surface px-2 font-mono text-sm text-text">
                    <option value="">chosen by seed</option>
                    {selected.eligible_steps.map((name) => <option key={name} value={name}>{name}</option>)}
                  </select>
                </label>
              )}
              <button type="submit" disabled={start.isPending || !scenarios.data.can_run}
                className="h-8 rounded bg-accent px-3 text-sm font-medium text-accent-text transition-opacity disabled:opacity-50">
                {start.isPending ? "Running…" : "Run scenario"} <span className="font-mono opacity-70">r</span>
              </button>
            </div>
            {!scenarios.data.can_run && <p className="text-xs text-muted">This server is read-only; runs cannot be started here.</p>}
            {start.isError && <ErrorState error={start.error} retry={() => start.reset()} />}
          </form>
          <h3 className="mt-8 text-base font-semibold">Recent runs of this scenario started here</h3>
          {mine.length === 0 ? (
            <EmptyState title="None yet">
              Run <Code>{selected.fault_type}</Code> with seed {effectiveSeed} and no controls to see the failure.
            </EmptyState>
          ) : (
            <table className="mt-2 w-full max-w-3xl border border-border text-left">
              <thead className="bg-surface text-xs">
                <tr className="h-row border-b border-border">
                  <th className="px-2">Run</th><th className="px-2">Controls</th><th className="px-2 text-right">Seed</th><th className="px-2">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {mine.map((run) => (
                  <tr key={run.run_id} className="h-row border-b border-border">
                    <td className="px-2 font-mono text-xs"><Link className="text-accent hover:underline" to={`/runs/${run.run_id}?batch=adhoc`}>{run.run_id}</Link></td>
                    <td className="px-2 font-mono text-xs">{controlsLabel(run.controls)}</td>
                    <td className="num px-2 text-right text-xs">{run.seed}</td>
                    <td className="px-2"><OutcomeBadge outcome={outcomeOf(run)} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          </div>
          <aside className="min-w-0" aria-label="Benchmark evidence for this fault">
            <h3 className="text-base font-semibold">
              What the latest benchmark batch shows for <span className="font-mono">{selected.fault_type}</span>
            </h3>
            {evidence.length === 0 ? (
              <EmptyState title="No benchmark runs for this scenario">
                Run <Code>make bench</Code> or use Run matrix on the Benchmark screen.
              </EmptyState>
            ) : (
              <>
                <p className="mt-1 text-xs text-muted">
                  Batch <span className="font-mono text-text">{bench.data?.batch_id}</span>, runs per controls config, by
                  verified outcome. Each row links to its runs.
                </p>
                <table className="mt-2 w-full table-fixed border border-border text-left">
                  <thead className="bg-surface text-xs">
                    <tr className="h-row border-b border-border">
                      <th className="px-2">Controls config</th>
                      <th className="w-12 px-2 text-right">Runs</th>
                      <th className="w-24 px-2 text-right">Verified</th>
                      <th className="px-2">Other outcomes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evidence.map((cell) => (
                      <tr key={cell.controls} className="h-row border-b border-border align-top">
                        <td className="px-2 py-2 font-mono text-xs">
                          <Link className="text-accent hover:underline"
                            to={`/runs?batch=${bench.data?.batch_id}&fault=${cell.fault_type}&controls=${encodeURIComponent(cell.controls)}`}>
                            {cell.controls.replace(/\+/g, " + ")}
                          </Link>
                        </td>
                        <td className="num px-2 py-2 text-right">{cell.runs}</td>
                        <td className="num whitespace-nowrap py-2 px-2 text-right">{cell.outcomes.verified_recovery} ({fixed(cell.verified_pct)}%)</td>
                        <td className="px-2">
                          <span className="flex flex-col py-1">
                            {OUTCOME_ORDER.filter((name) => name !== "verified_recovery" && cell.outcomes[name] > 0).map((name) => (
                              <span key={name} className={`flex items-start gap-1 text-xs ${OUTCOME_STYLE[name as Outcome].text}`}>
                                <span className="mt-0.5">{OUTCOME_STYLE[name as Outcome].icon}</span>
                                <span><span className="num">{cell.outcomes[name]}</span> {name.replace(/_/g, " ")}</span>
                              </span>
                            ))}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            {selected.eligible_steps.length > 0 && (
              <>
                <h3 className="mt-6 text-base font-semibold">Workflow steps this fault can hit</h3>
                <ol className="mt-2 border border-border">
                  {["create_order", "reserve_inventory", "create_invoice", "confirm_order", "notify_customer"].map((name, index) => (
                    <li key={name} className="flex h-row items-center gap-3 border-b border-border px-2 last:border-b-0">
                      <span className="num w-4 text-right text-xs text-muted">{index + 1}</span>
                      <span className="font-mono text-xs">{name}</span>
                      <span className="ml-auto text-xs text-muted">
                        {selected.eligible_steps.includes(name) ? "eligible" : "never targeted"}
                      </span>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}
