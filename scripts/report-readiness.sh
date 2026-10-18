#!/usr/bin/env bash
# Started in the background by brev-setup.sh. The sim prints a marker as it
# reaches each stage; report them in the order the backend requires, then
# report ready once the container's health check passes. On failure or
# timeout, report the stage that did not complete, with the sim's log tail.
set -uo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
compose=(docker compose -f compose.yaml -f compose.stream.yaml)
report() { python3 -I "$repo_dir/scripts/report-step.py" "$@" || true; }
sim_logs() { "${compose[@]}" logs --no-color --tail=400 sim 2>&1; }
# Markers print early and Isaac Sim logs heavily, so scan the whole log, not the tail.
all_sim_logs() { "${compose[@]}" logs --no-color sim 2>&1; }
timeout_seconds="${SARONIC_READY_TIMEOUT:-900}"

steps=(scene_loaded physics_running cameras_ready)
markers=(HarborSceneLoaded HarborPhysicsReady HarborCamerasReady)
reached=0

fail() {
  local failed_step="${steps[reached]:-ready}"
  sim_logs | report --log isaac_launch
  sim_logs | report failed --failed-step "$failed_step" --error-code "$1"
  exit 1
}

deadline=$((SECONDS + timeout_seconds))
while ((SECONDS < deadline)); do
  logs="$(all_sim_logs)"
  while ((reached < ${#steps[@]})) && grep -q "${markers[reached]}:" <<<"$logs"; do
    report "${steps[reached]}"
    reached=$((reached + 1))
  done
  container="$("${compose[@]}" ps -q sim 2>/dev/null)"
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null)"
  if [[ "$status" == unhealthy ]]; then
    fail SIM_UNHEALTHY
  fi
  if [[ "$status" == healthy ]] && ((reached == ${#steps[@]})); then
    sim_logs | report --log isaac_launch
    report ready
    exit 0
  fi
  sleep 10
done
fail STARTUP_TIMEOUT
