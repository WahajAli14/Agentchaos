"""Benchmark metrics. Every number is computed from rows in the results database."""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agentchaos.results.states import Classification
from agentchaos.results.store import ResultsStore

INDETERMINATE = "indeterminate"
UNFINISHED = "unfinished"
OUTCOME_COLUMNS = (*[c.value for c in Classification], INDETERMINATE)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in (0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def controls_label(controls: Sequence[str]) -> str:
    return "+".join(sorted(controls)) if controls else "none"


@dataclass
class Cell:
    fault_type: str
    controls: str
    runs: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    violations: Counter[str] = field(default_factory=Counter)
    duplicate_requests: int = 0
    duplicate_committed_operations: int = 0
    tool_calls: int = 0
    latencies: list[float] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)

    @property
    def verified(self) -> int:
        return self.outcomes[Classification.VERIFIED_RECOVERY.value]

    @property
    def verified_pct(self) -> float | None:
        return 100 * self.verified / self.runs if self.runs else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault_type": self.fault_type,
            "controls": self.controls,
            "runs": self.runs,
            "outcomes": {name: self.outcomes[name] for name in OUTCOME_COLUMNS},
            "verified_pct": self.verified_pct,
            "violations_by_type": dict(sorted(self.violations.items())),
            "invariant_violations": sum(self.violations.values()),
            "duplicate_requests": self.duplicate_requests,
            "duplicate_committed_operations": self.duplicate_committed_operations,
            "tool_calls": self.tool_calls,
            "latency_p50_ms": percentile(self.latencies, 50),
            "latency_p95_ms": percentile(self.latencies, 95),
            "run_ids": self.run_ids,
        }


def benchmark_matrix(store: ResultsStore, batch_id: str) -> list[Cell]:
    """One cell per (fault type, controls) for a batch, in first-seen order."""
    cells: dict[tuple[str, str], Cell] = {}
    by_run: dict[str, Cell] = {}
    for run in store.list_runs(batch_id):
        key = (run["fault_type"] or "no_fault", controls_label(run["controls"]))
        cell = cells.setdefault(key, Cell(*key))
        by_run[run["run_id"]] = cell
        cell.runs += 1
        cell.run_ids.append(run["run_id"])
        if run["final_state"] is None:
            cell.outcomes[UNFINISHED] += 1
            continue
        cell.outcomes[run["classification"] or INDETERMINATE] += 1
        cell.duplicate_requests += run["duplicate_requests"]
        cell.duplicate_committed_operations += run["duplicate_committed_operations"]
        cell.tool_calls += run["tool_calls"]
        if run["latency_ms"] is not None:
            cell.latencies.append(run["latency_ms"])
    for result in store.all_invariant_results(batch_id):
        if not result["passed"]:
            by_run[result["run_id"]].violations[result["invariant"]] += len(result["violations"])
    return list(cells.values())


def controls_delta(cells: Sequence[Cell], fault_type: str, on: str) -> dict[str, Any] | None:
    """Change from controls `none` to `on` for one fault type."""
    lookup = {(c.fault_type, c.controls): c for c in cells}
    off_cell, on_cell = lookup.get((fault_type, "none")), lookup.get((fault_type, on))
    if off_cell is None or on_cell is None:
        return None

    def diff(name: str) -> int:
        return int(on_cell.as_dict()[name]) - int(off_cell.as_dict()[name])

    return {
        "fault_type": fault_type,
        "off": "none",
        "on": on,
        "verified": on_cell.verified - off_cell.verified,
        "undetected_violation": on_cell.outcomes[Classification.UNDETECTED_VIOLATION.value]
        - off_cell.outcomes[Classification.UNDETECTED_VIOLATION.value],
        "invariant_violations": diff("invariant_violations"),
        "duplicate_committed_operations": diff("duplicate_committed_operations"),
    }


def format_matrix(cells: Sequence[Cell]) -> str:
    """Plain-text table of the matrix (counts, not rates, plus % verified)."""
    header = (
        f"{'fault':<22}{'controls':<46}{'n':>4}{'verified':>9}{'partial':>8}{'manual':>7}"
        f"{'undetect':>9}{'indet':>6}{'viol':>6}{'dupReq':>7}{'dupCommit':>10}{'p50ms':>7}"
    )
    lines = [header, "-" * len(header)]
    for cell in cells:
        d = cell.as_dict()
        o = d["outcomes"]
        lines.append(
            f"{cell.fault_type:<22}{cell.controls:<46}{cell.runs:>4}"
            f"{o['verified_recovery']:>9}{o['partial_recovery']:>8}"
            f"{o['manual_intervention_required']:>7}{o['undetected_violation']:>9}"
            f"{o['indeterminate']:>6}{d['invariant_violations']:>6}"
            f"{d['duplicate_requests']:>7}{d['duplicate_committed_operations']:>10}"
            f"{d['latency_p50_ms'] or 0:>7.0f}"
        )
    return "\n".join(lines)
