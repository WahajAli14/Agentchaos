import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/Layout";
import { Code, EmptyState, ErrorState, SkeletonBlock } from "../components/States";
import { api } from "../lib/api";
import { useKeys } from "../lib/useKeys";

export function InvariantsPage() {
  const { name } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const batch = params.get("batch") ?? undefined;
  const query = useQuery({ queryKey: ["invariants", batch ?? "latest"], queryFn: () => api.invariants(batch) });
  const list = query.data?.invariants ?? [];
  const selected = list.find((item) => item.invariant === name) ?? list[0];
  const move = (step: number) => {
    if (!selected) return;
    const next = list[list.indexOf(selected) + step];
    if (next) navigate({ pathname: `/invariants/${next.invariant}`, search: params.toString() });
  };
  useKeys({ j: () => move(1), k: () => move(-1) });

  if (query.isPending) return <SkeletonBlock lines={10} />;
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const batchId = query.data.batch_id;

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Invariants: what must be true in the service databases">
        {batchId && <span className="font-mono text-xs text-muted">batch {batchId}</span>}
      </PageHeader>
      {!batchId || !selected ? (
        <EmptyState title="No benchmark batch stored yet">
          Run <Code>make bench</Code> to check the five invariants across the fault matrix.
        </EmptyState>
      ) : (
        <div className="flex min-h-0 flex-1">
          <ul className="w-list shrink-0 overflow-auto border-r border-border" aria-label="Invariants">
            {list.map((item) => (
              <li key={item.invariant}>
                <Link to={{ pathname: `/invariants/${item.invariant}`, search: params.toString() }}
                  className={`flex items-start justify-between gap-3 border-b border-l-2 border-border px-3 py-2 transition-colors hover:bg-raised ${
                    item === selected ? "border-l-accent bg-raised" : "border-l-transparent"
                  }`}>
                  <span className="min-w-0">
                    <span className="block font-mono text-sm">{item.invariant}</span>
                    <span className="block text-xs text-muted">{item.description}</span>
                  </span>
                  <span className={`num shrink-0 text-right text-xs ${item.runs_violated ? "text-violation" : "text-verified"}`}>
                    {item.runs_violated}/{item.runs_checked}
                    <span className="block text-muted">runs violated</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
          <div className="min-w-0 flex-1 space-y-6 overflow-auto p-4">
            <div>
              <h2 className="font-mono text-lg font-semibold">{selected.invariant}</h2>
              <p className="mt-1 text-muted">{selected.description}</p>
              <p className="mt-2 text-xs text-muted">
                Violated in <span className="num text-text">{selected.runs_violated}</span> of{" "}
                <span className="num text-text">{selected.runs_checked}</span> runs in this batch,{" "}
                <span className="num text-text">{selected.violations}</span> violating records in total.
              </p>
            </div>
            <section>
              <h3 className="mb-2 text-base font-semibold">Where it was violated, by fault and controls config</h3>
              {selected.by_cell.length === 0 ? (
                <EmptyState title="Never violated in this batch" />
              ) : (
                <table className="w-full max-w-4xl border border-border text-left">
                  <thead className="bg-surface text-xs">
                    <tr className="h-row border-b border-border">
                      <th className="px-2">Fault</th>
                      <th className="px-2">Controls</th>
                      <th className="px-2 text-right">Runs violated</th>
                      <th className="px-2 text-right">Violating records</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.by_cell.map((cell) => (
                      <tr key={`${cell.fault_type}/${cell.controls}`} className="h-row border-b border-border">
                        <td className="px-2 font-mono text-xs">{cell.fault_type}</td>
                        <td className="px-2 font-mono text-xs">
                          <Link className="text-accent hover:underline"
                            to={`/runs?batch=${batchId}&fault=${cell.fault_type}&controls=${encodeURIComponent(cell.controls)}`}>
                            {cell.controls}
                          </Link>
                        </td>
                        <td className="num px-2 text-right">{cell.runs_violated}</td>
                        <td className="num px-2 text-right">{cell.violations}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
            {selected.examples.length > 0 && (
              <section>
                <h3 className="mb-2 text-base font-semibold">Example violating records</h3>
                <table className="w-full table-fixed border border-border text-left">
                  <thead className="bg-surface text-xs">
                    <tr className="h-row border-b border-border">
                      <th className="w-28 px-2">Run</th>
                      <th className="w-56 px-2">Record</th>
                      <th className="px-2">Detail from the database</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selected.examples.map((example) => (
                      <tr key={`${example.run_id}/${example.entity_id}`} className="border-b border-border align-top">
                        <td className="px-2 py-2 font-mono text-xs">
                          <Link className="text-accent hover:underline" to={`/runs/${example.run_id}?batch=${batchId}`}>{example.run_id}</Link>
                        </td>
                        <td className="px-2 py-2 font-mono text-xs">{example.entity_type} {example.entity_id}</td>
                        <td className="break-all px-2 py-2 font-mono text-xs text-muted">{JSON.stringify(example.detail)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
