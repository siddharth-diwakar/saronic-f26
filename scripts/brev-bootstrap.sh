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
  if [[ ! -f "$repo_dir/scripts/brev-setup.sh" ]]; then
    echo "Selected revision has no scripts/brev-setup.sh." >&2
    return 1
  fi
  echo "Starting Saronic from $revision ($(git -C "$repo_dir" rev-parse --short HEAD))"
  cd "$repo_dir"
  export SARONIC_REF="$revision"
  exec bash scripts/brev-setup.sh
}

bootstrap "$@"
