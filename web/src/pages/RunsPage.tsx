import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/Layout";
import { OutcomeBadge } from "../components/OutcomeBadge";
import { Code, EmptyState, ErrorState, SkeletonRows } from "../components/States";
import { SortIcon } from "../components/icons";
import { api, controlsLabel, outcomeOf, type Run } from "../lib/api";
import { useKeys } from "../lib/useKeys";
import { RunDetailPane } from "./RunDetailPane";

type SortKey = "run_id" | "fault_type" | "controls" | "seed" | "outcome";
const COLUMNS: { key: SortKey; label: string; numeric?: boolean }[] = [
  { key: "run_id", label: "Run" },
  { key: "fault_type", label: "Fault" },
  { key: "controls", label: "Controls" },
  { key: "seed", label: "Seed", numeric: true },
  { key: "outcome", label: "Outcome" },
];
const MAX_ROWS = 500;

function sortValue(run: Run, key: SortKey): string | number {
  if (key === "controls") return controlsLabel(run.controls);
  if (key === "outcome") return outcomeOf(run);
  if (key === "seed") return run.seed;
  return run[key] ?? "";
}

export function RunsPage() {
  const { runId } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const batch = params.get("batch") ?? "";
  const fault = params.get("fault") ?? "";
  const controls = params.get("controls") ?? "";
  const q = params.get("q") ?? "";
  const sort = (params.get("sort") as SortKey) ?? "run_id";
  const dir = params.get("dir") === "asc" ? "asc" : "desc";

  const runs = useQuery({ queryKey: ["runs", batch], queryFn: () => api.runs(batch || undefined) });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.batches });

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const rows = useMemo(() => {
    const needle = q.toLowerCase();
    const filtered = (runs.data ?? []).filter(
      (run) =>
        (!fault || (run.fault_type ?? "no_fault") === fault) &&
        (!controls || controlsLabel(run.controls) === controls) &&
        (!needle ||
          [run.run_id, run.scenario, run.fault_type ?? "", outcomeOf(run), String(run.seed)].some((field) =>
            field.toLowerCase().includes(needle),
          )),
    );
    const sign = dir === "asc" ? 1 : -1;
    return filtered.sort((a, b) => {
      const [x, y] = [sortValue(a, sort), sortValue(b, sort)];
      return x < y ? -sign : x > y ? sign : 0;
    });
  }, [runs.data, fault, controls, q, sort, dir]);

  const select = (id: string) => navigate({ pathname: `/runs/${id}`, search: params.toString() });

  // j/k move a cursor through the visible rows; Enter opens the row under it.
  const searchRef = useRef<HTMLInputElement>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  // The ref keeps rapid key presses correct between renders.
  const cursorRef = useRef<string | null>(null);
  const visible = rows.slice(0, MAX_ROWS);
  const moveCursor = (step: number) => {
    if (!visible.length) return;
    const current = visible.findIndex((run) => run.run_id === (cursorRef.current ?? runId));
    const next = visible[Math.min(visible.length - 1, Math.max(0, current + step))] ?? visible[0];
    cursorRef.current = next.run_id;
    setCursor(next.run_id);
    document.getElementById(`row-${next.run_id}`)?.scrollIntoView({ block: "nearest" });
  };
  useKeys({
    j: () => moveCursor(1),
    k: () => moveCursor(-1),
    Enter: () => cursorRef.current && select(cursorRef.current),
    "/": () => searchRef.current?.focus(),
  });

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Runs: every stored execution and its timeline">
        <label className="text-xs text-muted" htmlFor="batch">Batch</label>
        <select id="batch" value={batch} onChange={(e) => set("batch", e.target.value)}
          className="h-7 rounded border border-border bg-surface px-2 font-mono text-xs">
          <option value="">all batches</option>
          {batches.data?.map((b) => (
            <option key={b.batch_id} value={b.batch_id}>{b.batch_id} ({b.runs})</option>
          ))}
        </select>
        <input ref={searchRef} type="search" value={q} onChange={(e) => set("q", e.target.value)} aria-label="Filter runs"
          placeholder="Filter by id, fault, outcome, seed ( / )"
          className="h-7 w-64 rounded border border-border bg-surface px-2 text-xs placeholder:text-muted" />
      </PageHeader>
      <div className="flex min-h-0 flex-1">
        <div className="flex w-list shrink-0 flex-col border-r border-border">
          {(fault || controls) && (
            <div className="flex items-center gap-2 border-b border-border px-2 py-1 text-xs text-muted">
              Showing {fault && <Code>{fault}</Code>} {controls && <Code>{controls}</Code>}
              <button type="button" className="text-accent hover:underline"
                onClick={() => { const next = new URLSearchParams(params); next.delete("fault"); next.delete("controls"); setParams(next); }}>
                clear
              </button>
            </div>
          )}
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="w-full text-left">
              <thead className="sticky top-0 bg-surface text-xs">
                <tr className="h-row border-b border-border">
                  {COLUMNS.map((column) => (
                    <th key={column.key} className={`px-2 font-semibold ${column.numeric ? "text-right" : ""}`}
                      aria-sort={sort === column.key ? (dir === "asc" ? "ascending" : "descending") : "none"}>
                      <button type="button" className="inline-flex items-center gap-1 hover:text-accent"
                        onClick={() => {
                          const next = new URLSearchParams(params);
                          next.set("sort", column.key);
                          next.set("dir", sort === column.key && dir === "asc" ? "desc" : "asc");
                          setParams(next, { replace: true });
                        }}>
                        {column.label}
                        {sort === column.key && <SortIcon direction={dir} />}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              {runs.isPending ? (
                <SkeletonRows columns={COLUMNS.length} rows={14} />
              ) : (
                <tbody>
                  {visible.map((run) => (
                    <tr key={run.run_id} id={`row-${run.run_id}`} onClick={() => select(run.run_id)}
                      aria-selected={run.run_id === runId} data-cursor={run.run_id === cursor}
                      className={`h-row cursor-pointer border-b border-l-2 border-border transition-colors hover:bg-raised ${
                        run.run_id === runId ? "border-l-accent bg-raised" : "border-l-transparent"
                      } ${run.run_id === cursor ? "outline outline-1 -outline-offset-1 outline-accent" : ""}`}>
                      <td className="px-2 font-mono text-xs">
                        <a href={`/runs/${run.run_id}`} onClick={(e) => { e.preventDefault(); select(run.run_id); }}>
                          {run.run_id}
                        </a>
                      </td>
                      <td className="px-2 font-mono text-xs">{run.fault_type ?? "none"}</td>
                      <td className="max-w-0 truncate px-2 font-mono text-xs" title={controlsLabel(run.controls)}>
                        {controlsLabel(run.controls)}
                      </td>
                      <td className="num px-2 text-right text-xs">{run.seed}</td>
                      <td className="px-2"><OutcomeBadge outcome={outcomeOf(run)} /></td>
                    </tr>
                  ))}
                </tbody>
              )}
            </table>
            {runs.isError && <ErrorState error={runs.error} retry={() => runs.refetch()} />}
            {runs.isSuccess && runs.data.length === 0 && (
              <EmptyState title="No runs stored yet">
                Run scenario <Code>timeout_after_commit</Code> with seed 42 from Scenarios, or run{" "}
                <Code>make bench</Code>.
              </EmptyState>
            )}
            {runs.isSuccess && runs.data.length > 0 && rows.length === 0 && (
              <EmptyState title="No runs match these filters">Clear the filter or choose another batch.</EmptyState>
            )}
          </div>
          <div className="num border-t border-border px-2 py-1 text-xs text-muted">
            {rows.length > MAX_ROWS ? `first ${MAX_ROWS} of ${rows.length} runs` : `${rows.length} runs`}
          </div>
        </div>
        <div className="min-w-0 flex-1 overflow-auto">
          {runId ? (
            <RunDetailPane runId={runId} />
          ) : (
            <EmptyState title="No run selected">Select a run to see its three-lane timeline.</EmptyState>
          )}
        </div>
      </div>
    </div>
  );
}
