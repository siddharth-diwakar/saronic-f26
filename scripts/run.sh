#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
mkdir -p output
if command -v setfacl >/dev/null 2>&1; then
  setfacl -m "u:1234:rwx" -m "d:u:1234:rwx" -m "d:u:$(id -u):rwx" output
else
  # Isaac Sim's container user needs write access to this host directory.
  chmod a+rwx output
fi
docker compose build sim
docker compose run --rm sim "$@"
