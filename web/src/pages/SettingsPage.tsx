import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "../components/Layout";
import { ErrorState, SkeletonBlock } from "../components/States";
import { api } from "../lib/api";
import { SHORTCUTS } from "../lib/useKeys";

export function SettingsPage() {
  const query = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (query.isPending) return <SkeletonBlock lines={8} />;
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const s = query.data;
  const rows: [string, string, string][] = [
    ["AGENTCHAOS_ENV", s.env, "Environment name."],
    ["AGENTCHAOS_DEFAULT_SEED", String(s.default_seed), "First seed used by the benchmark and the Scenarios form."],
    ["AGENTCHAOS_LIVE_MODE_ENABLED", String(s.live_mode_enabled), "Whether real LLM runs may be started."],
    ["AGENTCHAOS_DATA_DIR", s.data_dir, "Where the mock service and results databases live."],
    ["Results database", s.results_db, `Append-only. ${s.runs_stored} runs stored.`],
    ["OTEL_EXPORTER_OTLP_ENDPOINT", s.otel_endpoint ?? "not set", "Where traces are sent when a collector is reachable."],
  ];
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Settings: server configuration (read-only, no secrets)" />
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        <section>
          <h2 className="mb-2 text-base font-semibold">Values this server was started with</h2>
          <table className="w-full max-w-5xl border border-border text-left">
            <thead className="bg-surface text-xs">
              <tr className="h-row border-b border-border">
                <th className="px-2">Setting</th><th className="px-2">Value</th><th className="px-2">Meaning</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([name, value, meaning]) => (
                <tr key={name} className="h-row border-b border-border">
                  <td className="px-2 font-mono text-xs">{name}</td>
                  <td className="num px-2 text-xs">{value}</td>
                  <td className="px-2 text-xs text-muted">{meaning}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-muted">
            Change these in <code className="font-mono">.env</code> and restart. API keys are never shown here; the Providers
            screen shows them masked.
          </p>
        </section>
        <section>
          <h2 className="mb-2 text-base font-semibold">Keyboard shortcuts</h2>
          <table className="w-full max-w-2xl border border-border text-left">
            <tbody>
              {SHORTCUTS.map((shortcut) => (
                <tr key={shortcut.keys} className="h-row border-b border-border">
                  <td className="w-32 px-2 font-mono text-xs">{shortcut.keys}</td>
                  <td className="px-2 text-xs">{shortcut.action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
