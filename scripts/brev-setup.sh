#!/bin/bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "$repo_dir/compose.yaml" ]]; then
  echo "Brev's source checkout is missing at $repo_dir." >&2
  exit 1
fi

cd "$repo_dir"
./scripts/check-host.sh
./scripts/run.sh deploy boat --x "${BOAT_X:-0}" --y "${BOAT_Y:-0}"
test -s output/boat_scene.usd
echo "Isaac Sim smoke scene ready at $repo_dir/output/boat_scene.usd"

public_ip="$(curl -4fsS https://ifconfig.me)"
export ISAACSIM_HOST="$public_ip"
if ! docker compose -f compose.yaml -f compose.stream.yaml up --build --force-recreate -d; then
  docker compose -f compose.yaml -f compose.stream.yaml ps
  docker compose -f compose.yaml -f compose.stream.yaml logs --no-color --tail=120 sim
  exit 1
fi
echo "Isaac Sim browser viewer starting at http://$public_ip:8210"
