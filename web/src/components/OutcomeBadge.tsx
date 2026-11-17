import type { ReactNode } from "react";
import type { Outcome } from "../lib/api";
import { OUTCOME_LABEL } from "../lib/format";
import { CheckIcon, CrossIcon, HalfIcon, HandIcon, UnknownIcon } from "./icons";

// Colour is never the only signal: every outcome has its own icon and its label.
export const OUTCOME_STYLE: Record<Outcome, { text: string; border: string; icon: ReactNode }> = {
  verified_recovery: { text: "text-verified", border: "border-l-verified", icon: <CheckIcon /> },
  partial_recovery: { text: "text-partial", border: "border-l-partial", icon: <HalfIcon /> },
  manual_intervention_required: { text: "text-manual", border: "border-l-manual", icon: <HandIcon /> },
  undetected_violation: { text: "text-violation", border: "border-l-violation", icon: <CrossIcon /> },
  indeterminate: { text: "text-indeterminate", border: "border-l-indeterminate", icon: <UnknownIcon /> },
  unfinished: { text: "text-muted", border: "border-l-border-strong", icon: <UnknownIcon /> },
};

export function OutcomeBadge({ outcome, count }: { outcome: Outcome; count?: number }) {
  const style = OUTCOME_STYLE[outcome];
  return (
    <span className={`inline-flex items-center gap-1 whitespace-nowrap ${style.text}`} data-outcome={outcome}>
      {style.icon}
      {count !== undefined && <span className="num">{count}</span>}
      <span className="font-mono text-xs">{OUTCOME_LABEL[outcome]}</span>
    </span>
  );
}
