#!/usr/bin/env bash
# Start the API on a throwaway data directory pre-filled by a real benchmark run.
set -euo pipefail
cd "$(dirname "$0")/../.."
export AGENTCHAOS_DATA_DIR="$(mktemp -d)"
export PYTHONPATH=src
uv run python -m agentchaos.bench --seeds 20 --data-dir "$AGENTCHAOS_DATA_DIR" > /dev/null
exec uv run uvicorn agentchaos.api:app --factory --port 8123 --log-level warning
