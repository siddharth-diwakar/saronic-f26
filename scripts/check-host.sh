#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is missing. Install Docker Engine on the Brev instance." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is missing. Install the Compose plugin." >&2
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi is missing. Use a Brev GPU instance with an NVIDIA driver." >&2
  exit 1
fi

echo "GPU:"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo "Container GPU access:"
docker info --format '{{json .Runtimes}}' | grep -q nvidia || {
  echo "NVIDIA Docker runtime was not found. Install/configure NVIDIA Container Toolkit." >&2
  exit 1
}

echo "Host checks passed."
