#!/bin/bash
set -euo pipefail

repo_url="https://github.com/siddharth-diwakar/saronic-f26.git"
repo_ref="feature/isaac-sim-docker"
repo_dir="${HOME}/workspace/saronic-f26"

mkdir -p "$(dirname "$repo_dir")"
if [[ ! -d "$repo_dir/.git" ]]; then
  if [[ -e "$repo_dir" ]]; then
    echo "Cannot clone: $repo_dir already exists and is not a Git repository." >&2
    exit 1
  fi
  git clone --branch "$repo_ref" --single-branch "$repo_url" "$repo_dir"
else
  git -C "$repo_dir" fetch origin "$repo_ref"
  git -C "$repo_dir" switch "$repo_ref"
  git -C "$repo_dir" pull --ff-only origin "$repo_ref"
fi

cd "$repo_dir"
./scripts/check-host.sh
./scripts/run.sh deploy boat --x "${BOAT_X:-0}" --y "${BOAT_Y:-0}"
test -s output/boat_scene.usd
echo "Isaac Sim smoke scene ready at $repo_dir/output/boat_scene.usd"
