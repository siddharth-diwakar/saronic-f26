#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
: "${ISAACSIM_HOST:?Set ISAACSIM_HOST to the Brev public IP}"
./scripts/prepare-output.sh
./scripts/prepare-volumes.sh
rm -f output/.harbor-ready
docker compose -f compose.yaml -f compose.stream.yaml up --build --force-recreate -d
printf 'Harbor viewer: http://%s:8210\n' "$ISAACSIM_HOST"
echo 'Select Overview, Forward, or Mast in Boat controls. Use the sliders to drive.'
