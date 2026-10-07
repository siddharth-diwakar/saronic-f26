#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
./scripts/prepare-output.sh
docker compose build sim
./scripts/prepare-volumes.sh
docker compose run --rm sim "$@"
