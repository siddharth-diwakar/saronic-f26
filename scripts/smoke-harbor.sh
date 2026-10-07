#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
if [[ -n "$(docker compose ps --status running -q sim)" ]]; then
  echo 'Stop the streaming sim before this check to free the GPU. See README.' >&2
  exit 1
fi
./scripts/run.sh harbor --duration 10 --record --output /workspace/output/smoke-idle
./scripts/run.sh harbor --duration 8 --record --throttle 0.4 --steering 0.15 --output /workspace/output/smoke-powered
docker compose run --rm --entrypoint /isaac-sim/python.sh sim /workspace/src/saronic_sim/verify_run.py /workspace/output/smoke-idle
docker compose run --rm --entrypoint /isaac-sim/python.sh sim /workspace/src/saronic_sim/verify_run.py /workspace/output/smoke-powered --powered
