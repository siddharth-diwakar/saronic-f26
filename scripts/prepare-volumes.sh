#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

# Docker creates new named volumes as root. Isaac Sim runs as UID 1234 and
# needs to write its texture cache, settings, and Kit logs on first launch.
docker compose run --rm --user 0:0 --entrypoint /bin/bash sim -lc '
  set -euo pipefail
  marker=/isaac-sim/.cache/.saronic-volumes-owned-v1
  if [[ ! -e "$marker" ]]; then
    chown -R 1234:1234 \
      /isaac-sim/.cache \
      /isaac-sim/.nv/ComputeCache \
      /isaac-sim/kit/cache \
      /isaac-sim/.nvidia-omniverse/logs \
      /isaac-sim/.nvidia-omniverse/config \
      /isaac-sim/.local/share/ov/data \
      /isaac-sim/.local/share/ov/pkg
    touch "$marker"
    chown 1234:1234 "$marker"
  fi
'
