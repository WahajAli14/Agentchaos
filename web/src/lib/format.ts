export const OUTCOME_LABEL: Record<string, string> = {
  verified_recovery: "verified_recovery",
  partial_recovery: "partial_recovery",
  manual_intervention_required: "manual_intervention_required",
  undetected_violation: "undetected_violation",
  indeterminate: "indeterminate",
  unfinished: "unfinished",
};

export const OUTCOME_ORDER = [
  "verified_recovery",
  "partial_recovery",
  "manual_intervention_required",
  "undetected_violation",
  "indeterminate",
] as const;

export function fixed(value: number | null | undefined, digits = 0): string {
  return value === null || value === undefined ? "–" : value.toFixed(digits);
}

export function signed(value: number): string {
  return value > 0 ? `+${value}` : String(value);
}

export function usd(value: number | null | undefined): string {
  return value === null || value === undefined ? "unknown" : `$${value.toFixed(6)}`;
}
