#!/bin/bash
set -euo pipefail

repo_dir="${HOME}/saronic-f26"

if [[ ! -f "$repo_dir/compose.yaml" ]]; then
  echo "Brev's source checkout is missing at $repo_dir." >&2
  exit 1
fi

# Override for a reviewed tag or branch; default to this feature during bring-up.
revision="${SARONIC_REF:-tanush/boat-harbor-simulation}"
git -C "$repo_dir" fetch origin "$revision"
git -C "$repo_dir" switch --detach FETCH_HEAD
cd "$repo_dir"
./scripts/check-host.sh
public_ip="$(curl -4fsS https://ifconfig.me)"
export ISAACSIM_HOST="$public_ip"
if ! ./scripts/start-harbor.sh; then
  docker compose -f compose.yaml -f compose.stream.yaml ps
  docker compose -f compose.yaml -f compose.stream.yaml logs --no-color --tail=120 sim
  exit 1
fi
echo "Harbor scenario opens automatically; select Forward or Mast in Boat controls."
echo "Isaac Sim browser viewer starting at http://$public_ip:8210"
