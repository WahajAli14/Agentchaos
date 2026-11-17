import type { ReactNode } from "react";
import { WarnIcon } from "./icons";

export function SkeletonRows({ rows = 8, columns }: { rows?: number; columns: number }) {
  return (
    <tbody aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r} className="h-row border-b border-border">
          {Array.from({ length: columns }, (_, c) => (
            <td key={c} className="px-2">
              <div className="skeleton h-3" style={{ width: `${40 + ((r * 7 + c * 13) % 50)}%` }} />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  );
}

export function SkeletonBlock({ lines = 6 }: { lines?: number }) {
  return (
    <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton h-3" style={{ width: `${55 + ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="p-4 text-sm">
      <p className="font-medium">{title}</p>
      {children && <p className="mt-1 text-muted">{children}</p>}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div className="m-4 flex items-start gap-2 rounded border border-violation p-3 text-sm" role="alert">
      <span className="mt-0.5 text-violation"><WarnIcon /></span>
      <div>
        <p className="font-mono text-xs">{error instanceof Error ? error.message : String(error)}</p>
        <button type="button" onClick={retry} className="mt-2 rounded border border-border-strong px-2 py-1 text-xs hover:bg-raised">
          Retry
        </button>
      </div>
    </div>
  );
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="rounded-sm bg-raised px-1 font-mono text-xs">{children}</code>;
}
