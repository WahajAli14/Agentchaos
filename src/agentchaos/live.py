"""Live-mode runner: a real LLM agent against the fault proxy.

    python -m agentchaos.live smoke --runs 3
    python -m agentchaos.live replay data/traces/run_000123.json --controls all

Smoke runs are a demonstration, not a measurement. With n this small no rate or
percentage is reported, and results are always shown per model.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from agentchaos.bench import ALL_CONTROLS
from agentchaos.config import Settings, get_settings
from agentchaos.engine import Engine, RunConfig, RunSummary
from agentchaos.faults import FAULT_TYPES
from agentchaos.interfaces import CallNext, WorkflowSpec
from agentchaos.live_agent import PROVIDER_ERROR, LiveAgent, ReplayAgent
from agentchaos.llm import LLMProvider
from agentchaos.llm_pool import ProviderPool
from agentchaos.metrics import controls_label
from agentchaos.proxy import Recorder
from agentchaos.results.store import ResultsStore
from agentchaos.services.cluster import ServiceCluster

SMOKE_CONTROLS: tuple[tuple[str, ...], ...] = ((), ALL_CONTROLS)


def save_trace(
    traces_dir: Path, summary: RunSummary, config: RunConfig, provider: LLMProvider
) -> Path:
    traces_dir.mkdir(parents=True, exist_ok=True)
    path = traces_dir / f"{summary.run_id}.json"
    path.write_text(
        json.dumps(
            {
                "run_id": summary.run_id,
                "provider": provider.name,
                "model": provider.model,
                "seed": config.seed,
                "fault_type": config.fault_type,
                "target_step": summary.target_step,
                "controls": list(config.controls),
                "trace": summary.trace,
            },
            indent=2,
        )
    )
    return path


async def run_live(
    engine: Engine, provider: LLMProvider, config: RunConfig, traces_dir: Path | None = None
) -> RunSummary:
    def factory(spec: WorkflowSpec, send: CallNext, recorder: Recorder, run_id: str) -> LiveAgent:
        return LiveAgent(provider, spec, send, recorder, run_id)

    summary = await engine.run(config, agent_factory=factory, provider=provider.config)
    if traces_dir is not None:
        save_trace(traces_dir, summary, config, provider)
    return summary


async def replay_trace(
    engine: Engine, recorded: dict[str, Any], controls: tuple[str, ...], batch_id: str | None = None
) -> RunSummary:
    """Re-run a recorded live run's decisions, without an LLM, under `controls`."""
    config = RunConfig(
        scenario=f"replay/{recorded['run_id']}/{controls_label(controls)}",
        seed=recorded["seed"],
        fault_type=recorded["fault_type"],
        target_step=recorded["target_step"],
        controls=controls,
        batch_id=batch_id,
        mode="replay",
    )

    def factory(spec: WorkflowSpec, send: CallNext, recorder: Recorder, run_id: str) -> ReplayAgent:
        return ReplayAgent(recorded["trace"], spec, send, recorder, run_id)

    return await engine.run(config, agent_factory=factory)


async def smoke(settings: Settings, runs: int) -> int:
    if not settings.live_mode_enabled:
        print("Live mode is off. Set AGENTCHAOS_LIVE_MODE_ENABLED=true in .env.", file=sys.stderr)
        return 2
    data_dir = settings.data_dir
    async with httpx.AsyncClient() as http:
        pool = ProviderPool.from_settings(settings, http)
        await pool.health_check()
        for row in pool.status():
            state = "healthy" if row["healthy"] else row["reason"]
            print(f"  provider {row['name']}: {state}")
        if pool.select() is None:
            print(
                "No healthy LLM provider. Put one provider key in .env "
                "(GROQ_API_KEY is the default) or start Ollama.",
                file=sys.stderr,
            )
            return 2
        store = ResultsStore(settings.results_db_path)
        cluster = ServiceCluster(settings.services_dir, seed=settings.default_seed)
        engine = Engine(cluster, store, data_dir / "control")
        batch_id = f"live_{store.count_runs() + 1:06d}"
        tally: dict[tuple[str, str, str], Counter[str]] = {}
        try:
            for fault_type in FAULT_TYPES:
                for controls in SMOKE_CONTROLS:
                    label = controls_label(controls)
                    for k in range(runs):
                        # One provider per run; a failed provider is skipped next time.
                        provider = pool.select()
                        if provider is None:
                            print("  no healthy provider left; stopping", file=sys.stderr)
                            break
                        model = f"{provider.name}/{provider.model}"
                        summary = await run_live(
                            engine,
                            provider,
                            RunConfig(
                                scenario=f"live/{fault_type}/{label}",
                                seed=settings.default_seed + k,
                                fault_type=fault_type,
                                controls=controls,
                                batch_id=batch_id,
                                mode="live",
                            ),
                            data_dir / "traces",
                        )
                        outcome = (
                            summary.classification.value
                            if summary.classification
                            else summary.state.value
                        )
                        if summary.trace and summary.trace[-1].get("believed_outcome") == (
                            PROVIDER_ERROR
                        ):
                            outcome = f"provider_aborted ({outcome} in the databases)"
                        tally.setdefault((model, fault_type, label), Counter())[outcome] += 1
                        print(f"  {summary.run_id} {model} {fault_type} [{label}] -> {outcome}")
        finally:
            engine.close()
            cluster.close()
            store.close()
    print(f"\nbatch {batch_id}; results are per model and never combined across models")
    for (model, fault_type, label), counts in tally.items():
        n = sum(counts.values())
        detail = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
        print(f"  {model}  {fault_type:<22}{label:<40}{detail}")
        print(f"    smoke test, n={n}, not statistically meaningful")
    for event in pool.failover_log():
        print(f"  failover: skipped {event['skipped']} ({event['reason']}) -> {event['chosen']}")
    return 0


async def replay(settings: Settings, trace_file: Path, controls: tuple[str, ...]) -> int:
    store = ResultsStore(settings.results_db_path)
    cluster = ServiceCluster(settings.services_dir, seed=settings.default_seed)
    engine = Engine(cluster, store, settings.data_dir / "control")
    try:
        summary = await replay_trace(engine, json.loads(trace_file.read_text()), controls)
    finally:
        engine.close()
        cluster.close()
        store.close()
    outcome = summary.classification.value if summary.classification else summary.state.value
    print(f"{summary.run_id} replay [{controls_label(controls)}] -> {outcome}")
    print(summary.explanation)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    smoke_cmd = commands.add_parser("smoke", help="live runs per fault, controls off and on")
    smoke_cmd.add_argument("--runs", type=int, default=3)
    replay_cmd = commands.add_parser("replay", help="replay a recorded trace without an LLM")
    replay_cmd.add_argument("trace", type=Path)
    replay_cmd.add_argument("--controls", default="none", help="none | all | comma-separated")
    args = parser.parse_args()
    settings = get_settings()
    if args.command == "smoke":
        raise SystemExit(asyncio.run(smoke(settings, args.runs)))
    controls: tuple[str, ...]
    if args.controls == "none":
        controls = ()
    elif args.controls == "all":
        controls = ALL_CONTROLS
    else:
        controls = tuple(args.controls.split(","))
    raise SystemExit(asyncio.run(replay(settings, args.trace, controls)))


if __name__ == "__main__":
    main()
