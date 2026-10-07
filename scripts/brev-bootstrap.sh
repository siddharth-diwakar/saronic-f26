#!/bin/bash
# This small script is the Launchable's VM setup script. Runtime logic lives
# in scripts/brev-setup.sh at the revision selected below.
set -euo pipefail

bootstrap() {
  local repo_dir="${SARONIC_REPO_DIR:-${HOME}/saronic-f26}"
  local revision="${SARONIC_REF:-main}"
  if [[ ! -d "$repo_dir/.git" ]]; then
    echo "Brev's source checkout is missing at $repo_dir." >&2
    return 1
  fi
  # Accept branch/tag names, including explicit refs to disambiguate names.
  case "$revision" in
    refs/heads/*|refs/tags/*) git check-ref-format "$revision" ;;
    refs/*|-*) echo "SARONIC_REF must be a branch or tag." >&2; return 1 ;;
    *) git check-ref-format "refs/heads/$revision" ;;
  esac
  if [[ -n "$(git -C "$repo_dir" status --porcelain)" ]]; then
    echo "Checkout has local changes; refusing to replace them." >&2
    return 1
  fi
  git -C "$repo_dir" fetch --no-tags origin "$revision"
  git -C "$repo_dir" switch --detach FETCH_HEAD
  echo "Starting Saronic from $revision ($(git -C "$repo_dir" rev-parse --short HEAD))"
  cd "$repo_dir"
  export SARONIC_REF="$revision"
  if [[ -f scripts/brev-setup.sh ]]; then
    exec bash scripts/brev-setup.sh
  fi

  # TEMPORARY: remove after the refactor is merged and test branches include it.
  # Preserve the selected checkout; never silently switch back to main/a tag.
  echo "Repo startup script missing; using temporary starter fallback."
  for required in scripts/check-host.sh scripts/run.sh compose.yaml compose.stream.yaml; do
    if [[ ! -f "$required" ]]; then
      echo "Temporary starter fallback requires $required at $revision." >&2
      return 1
    fi
  done
  bash scripts/check-host.sh
  bash scripts/run.sh deploy boat --x "${BOAT_X:-0}" --y "${BOAT_Y:-0}"
  test -s output/boat_scene.usd
  export ISAACSIM_HOST="${ISAACSIM_HOST:-$(curl -4fsS https://ifconfig.me)}"
  if ! docker compose -f compose.yaml -f compose.stream.yaml up --build --force-recreate -d; then
    docker compose -f compose.yaml -f compose.stream.yaml ps
    docker compose -f compose.yaml -f compose.stream.yaml logs --no-color --tail=120 sim
    return 1
  fi
  echo "Isaac Sim browser viewer starting at http://$ISAACSIM_HOST:8210"
}

bootstrap "$@"
