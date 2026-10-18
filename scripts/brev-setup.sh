#!/bin/bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# No-op unless the backend passed SARONIC_BACKEND_URL/SESSION_ID/INGEST_TOKEN.
report() { python3 -I "$repo_dir/scripts/report-step.py" "$@" || true; }

if [[ ! -f "$repo_dir/compose.yaml" ]]; then
  echo "Brev's source checkout is missing at $repo_dir." >&2
  report failed --error-code CHECKOUT_MISSING --failed-step instance_up </dev/null
  exit 1
fi

cd "$repo_dir"
report instance_up
if ! host_check="$(./scripts/check-host.sh 2>&1)"; then
  printf '%s\n' "$host_check" >&2
  printf '%s\n' "$host_check" | report failed --error-code HOST_CHECK_FAILED --failed-step container_built
  exit 1
fi
printf '%s\n' "$host_check"
public_ip="$(curl -4fsS https://ifconfig.me)"
export ISAACSIM_HOST="$public_ip"
if ! ./scripts/start-harbor.sh; then
  diagnostics="$(
    docker compose -f compose.yaml -f compose.stream.yaml ps 2>&1
    docker compose -f compose.yaml -f compose.stream.yaml logs --no-color --tail=120 sim 2>&1
  )"
  printf '%s\n' "$diagnostics"
  printf '%s\n' "$diagnostics" | report failed --error-code CONTAINER_START_FAILED --failed-step container_built
  exit 1
fi
report container_built
if [[ -n "${SARONIC_BACKEND_URL:-}" && -n "${SARONIC_SESSION_ID:-}" && -n "${SARONIC_INGEST_TOKEN:-}" ]]; then
  # Readiness takes minutes; report it without holding up the Launchable setup.
  nohup ./scripts/report-readiness.sh >output/report-readiness.log 2>&1 &
fi
echo "Harbor scenario opens automatically; select Forward or Mast in Boat controls."
echo "Isaac Sim browser viewer starting at http://$public_ip:8210"
