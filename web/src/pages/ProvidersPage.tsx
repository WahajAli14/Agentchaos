import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "../components/Layout";
import { Code, EmptyState, ErrorState, SkeletonBlock } from "../components/States";
import { CheckIcon, CrossIcon, UnknownIcon } from "../components/icons";
import { api, type ProviderStatus } from "../lib/api";
import { fixed, usd } from "../lib/format";

function Health({ provider }: { provider: ProviderStatus }) {
  if (!provider.enabled) {
    return <span className="inline-flex items-center gap-1 text-muted"><CrossIcon /> disabled</span>;
  }
  if (!provider.health_checked) {
    return <span className="inline-flex items-center gap-1 text-indeterminate"><UnknownIcon /> not checked</span>;
  }
  return provider.healthy ? (
    <span className="inline-flex items-center gap-1 text-verified"><CheckIcon /> healthy</span>
  ) : (
    <span className="inline-flex items-center gap-1 text-violation"><CrossIcon /> unhealthy</span>
  );
}

export function ProvidersPage() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const check = useMutation({
    mutationFn: api.checkProviders,
    onSuccess: () => client.invalidateQueries({ queryKey: ["providers"] }),
  });
  if (query.isPending) return <SkeletonBlock lines={10} />;
  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />;
  const { providers, failovers, live_models: models } = query.data;
  const anyEnabled = providers.some((p) => p.enabled);

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Providers: LLM health, limits and failover">
        <button type="button" onClick={() => check.mutate()} disabled={check.isPending || !anyEnabled}
          className="h-7 rounded bg-accent px-3 text-xs font-medium text-accent-text transition-opacity disabled:opacity-50">
          {check.isPending ? "Probing…" : "Run health check"}
        </button>
      </PageHeader>
      <div className="min-h-0 flex-1 space-y-6 overflow-auto p-4">
        {check.isError && <ErrorState error={check.error} retry={() => check.reset()} />}
        <section>
          <h2 className="mb-2 text-base font-semibold">Configured providers, in routing order</h2>
          {providers.length === 0 ? (
            <EmptyState title="No provider pool on this server" />
          ) : (
            <table className="w-full border border-border text-left">
              <thead className="bg-surface text-xs">
                <tr className="h-row border-b border-border">
                  <th className="px-2 text-right">Priority</th>
                  <th className="px-2">Provider</th>
                  <th className="px-2">Model</th>
                  <th className="px-2">Key</th>
                  <th className="px-2">Health</th>
                  <th className="px-2">Reason</th>
                  <th className="px-2">Circuit</th>
                  <th className="px-2 text-right">Requests, last minute</th>
                  <th className="px-2 text-right">Requests, last day</th>
                  <th className="px-2 text-right">Calls</th>
                  <th className="px-2 text-right">Failures</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((p) => (
                  <tr key={p.name} className="h-row border-b border-border">
                    <td className="num px-2 text-right">{p.priority ?? "–"}</td>
                    <td className="px-2 font-mono text-xs">{p.name}</td>
                    <td className="px-2 font-mono text-xs">{p.model ?? "–"}</td>
                    <td className="px-2 font-mono text-xs">{p.masked_key || (p.enabled ? "not needed" : "–")}</td>
                    <td className="px-2 text-xs"><Health provider={p} /></td>
                    <td className="px-2 text-xs text-muted">{p.reason ?? ""}</td>
                    <td className="px-2 font-mono text-xs">{p.circuit ?? "–"}</td>
                    <td className="num px-2 text-right">
                      {p.rate_limit ? `${p.rate_limit.used_last_minute} / ${p.rate_limit.rpm ?? "no limit"}` : "–"}
                    </td>
                    <td className="num px-2 text-right">
                      {p.rate_limit ? `${p.rate_limit.used_last_day} / ${p.rate_limit.rpd ?? "no limit"}` : "–"}
                    </td>
                    <td className="num px-2 text-right">{p.calls}</td>
                    <td className="num px-2 text-right">{p.failures}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!anyEnabled && providers.length > 0 && (
            <p className="mt-2 text-xs text-muted">
              Every provider is disabled. Put one key in <Code>.env</Code> (for example <Code>GROQ_API_KEY</Code>) or start
              Ollama, then restart the server. Keys are shown masked and never leave the server.
            </p>
          )}
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold">Failover events since this server started</h2>
          {failovers.length === 0 ? (
            <EmptyState title="No failover yet">A provider is skipped when it is unhealthy, rate limited or its circuit is open.</EmptyState>
          ) : (
            <table className="w-full max-w-3xl border border-border text-left">
              <thead className="bg-surface text-xs">
                <tr className="h-row border-b border-border">
                  <th className="px-2">Skipped provider</th><th className="px-2">Reason</th><th className="px-2">Routed to</th>
                </tr>
              </thead>
              <tbody>
                {failovers.map((event, index) => (
                  <tr key={index} className="h-row border-b border-border">
                    <td className="px-2 font-mono text-xs">{event.skipped}</td>
                    <td className="px-2 text-xs">{event.reason}</td>
                    <td className="px-2 font-mono text-xs">{event.chosen ?? "no provider available"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section>
          <h2 className="mb-2 text-base font-semibold">Stored live runs, per model</h2>
          {models.length === 0 ? (
            <EmptyState title="No live runs stored">
              Set <Code>AGENTCHAOS_LIVE_MODE_ENABLED=true</Code> and a provider key in <Code>.env</Code>, then run{" "}
              <Code>make live RUNS=3</Code>.
            </EmptyState>
          ) : (
            <table className="w-full border border-border text-left">
              <thead className="bg-surface text-xs">
                <tr className="h-row border-b border-border">
                  <th className="px-2">Model</th>
                  <th className="px-2 text-right">Runs</th>
                  <th className="px-2 text-right">Verified runs</th>
                  <th className="px-2 text-right">LLM calls</th>
                  <th className="px-2 text-right">Tokens in</th>
                  <th className="px-2 text-right">Tokens out</th>
                  <th className="px-2 text-right">Tokens per verified run</th>
                  <th className="px-2 text-right">Billed</th>
                  <th className="px-2 text-right">Estimated at list price</th>
                  <th className="px-2 text-right">Latency p50 (ms)</th>
                  <th className="px-2">Sample</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={`${m.provider}/${m.model}`} className="h-row border-b border-border">
                    <td className="px-2 font-mono text-xs">{m.provider}/{m.model}</td>
                    <td className="num px-2 text-right">{m.runs}</td>
                    <td className="num px-2 text-right">{m.verified_runs}</td>
                    <td className="num px-2 text-right">{m.llm_calls}</td>
                    <td className="num px-2 text-right">{m.prompt_tokens}</td>
                    <td className="num px-2 text-right">{m.completion_tokens}</td>
                    <td className="num px-2 text-right">{fixed(m.tokens_per_verified_run)}</td>
                    <td className="num px-2 text-right">{usd(m.billed_cost_usd)}</td>
                    <td className="num px-2 text-right">{usd(m.estimated_cost_usd)}</td>
                    <td className="num px-2 text-right">{fixed(m.latency_p50_ms)}</td>
                    <td className="px-2 text-xs text-muted">{m.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}
