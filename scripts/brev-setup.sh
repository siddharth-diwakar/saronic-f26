#!/bin/bash
set -euo pipefail

repo_dir="${HOME}/saronic-f26"

if [[ ! -f "$repo_dir/compose.yaml" ]]; then
  echo "Brev's source checkout is missing at $repo_dir." >&2
  exit 1
fi

git -C "$repo_dir" fetch origin tag brev-v1
git -C "$repo_dir" switch --detach brev-v1
cd "$repo_dir"
./scripts/check-host.sh
./scripts/run.sh deploy boat --x "${BOAT_X:-0}" --y "${BOAT_Y:-0}"
test -s output/boat_scene.usd
echo "Isaac Sim smoke scene ready at $repo_dir/output/boat_scene.usd"

public_ip="$(curl -4fsS https://ifconfig.me)"
ISAACSIM_HOST="$public_ip" docker compose -f compose.yaml -f compose.stream.yaml up --build -d
echo "Isaac Sim browser viewer starting at http://$public_ip:8210"
