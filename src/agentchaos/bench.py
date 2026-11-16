"""Benchmark runner: {fault type} x {controls config} x {N seeds}, deterministic mode.

    python -m agentchaos.bench --seeds 20

Results are appended to the results database under a new batch id; nothing is
ever overwritten. The printed matrix is computed by reading that batch back.
"""

import argparse
import asyncio
from pathlib import Path

from agentchaos.config import get_settings
from agentchaos.engine import Engine, RunConfig
from agentchaos.faults import FAULT_TYPES
from agentchaos.metrics import benchmark_matrix, controls_delta, controls_label, format_matrix
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

ALL_CONTROLS = ("idempotency", "saga", "verify_after_write")
# none / each individually / all, plus keys-without-server-support as evidence
# for the server-cooperation requirement.
CONTROL_CONFIGS: tuple[tuple[str, ...], ...] = (
    (),
    ("idempotency_client_only",),
    ("idempotency",),
    ("verify_after_write",),
    ("saga",),
    ALL_CONTROLS,
)


async def run_benchmark(data_dir: Path, seeds: int, start_seed: int) -> tuple[str, ResultsStore]:
    store = ResultsStore(data_dir / "results" / "results.db")
    cluster = ServiceCluster(data_dir / "services", seed=start_seed)
    engine = Engine(cluster, store, data_dir / "control")
    batch_id = f"batch_{store.count_runs() + 1:06d}"
    try:
        for fault_type in FAULT_TYPES:
            for controls in CONTROL_CONFIGS:
                for seed in range(start_seed, start_seed + seeds):
                    await engine.run(
                        RunConfig(
                            scenario=f"{fault_type}/{controls_label(controls)}",
                            seed=seed,
                            fault_type=fault_type,
                            controls=controls,
                            batch_id=batch_id,
                        )
                    )
    finally:
        engine.close()
        cluster.close()
    return batch_id, store


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=20, help="seeds per matrix cell")
    parser.add_argument("--start-seed", type=int, default=settings.default_seed)
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir)
    args = parser.parse_args()

    batch_id, store = asyncio.run(run_benchmark(args.data_dir, args.seeds, args.start_seed))
    cells = benchmark_matrix(store, batch_id)
    print(
        f"batch {batch_id}: {sum(c.runs for c in cells)} runs, "
        f"seeds {args.start_seed}..{args.start_seed + args.seeds - 1}, deterministic mode"
    )
    print(f"results database: {store.db_path}\n")
    print(format_matrix(cells))
    print("\ncontrols off -> all on (change in run counts):")
    for fault_type in FAULT_TYPES:
        delta = controls_delta(cells, fault_type, controls_label(ALL_CONTROLS))
        if delta:
            print(
                f"  {fault_type:<22} verified {delta['verified']:+d}, "
                f"undetected violations {delta['undetected_violation']:+d}, "
                f"invariant violations {delta['invariant_violations']:+d}, "
                f"duplicate committed operations {delta['duplicate_committed_operations']:+d}"
            )
    store.close()


if __name__ == "__main__":
    main()
