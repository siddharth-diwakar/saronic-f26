#!/usr/bin/env bash
# Local Git fixtures verify selection without network access or a GPU.
set -euo pipefail
bootstrap_script="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/brev-bootstrap.sh"
fixture="$(mktemp -d)"
trap 'rm -rf "$fixture"' EXIT
export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_AUTHOR_NAME='Bootstrap Test' GIT_AUTHOR_EMAIL='test@example.invalid'
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
git init --quiet --initial-branch=main "$fixture/source"
mkdir -p "$fixture/source/scripts"
cat > "$fixture/source/scripts/brev-setup.sh" <<'SETUP'
#!/usr/bin/env bash
printf 'STARTUP=main REF=%s SHA=%s BOAT_X=%s\n' "$SARONIC_REF" "$(git rev-parse HEAD)" "${BOAT_X:-unset}"
SETUP
git -C "$fixture/source" add .
git -C "$fixture/source" commit --quiet -m 'Main startup'
main_sha="$(git -C "$fixture/source" rev-parse HEAD)"
git -C "$fixture/source" tag test-release
git -C "$fixture/source" switch --quiet -c tanush/test-feature
sed 's/STARTUP=main/STARTUP=feature/' "$fixture/source/scripts/brev-setup.sh" > "$fixture/new-setup"
mv "$fixture/new-setup" "$fixture/source/scripts/brev-setup.sh"
git -C "$fixture/source" commit --quiet -am 'Feature startup'
feature_sha="$(git -C "$fixture/source" rev-parse HEAD)"
git -C "$fixture/source" switch --quiet -c no-startup main
git -C "$fixture/source" rm --quiet scripts/brev-setup.sh
mkdir -p "$fixture/source/scripts"
cat > "$fixture/source/scripts/check-host.sh" <<'CHECK'
echo 'FALLBACK_HOST_CHECK'
CHECK
cat > "$fixture/source/scripts/run.sh" <<'RUN'
mkdir -p output
printf 'fixture USD' > output/boat_scene.usd
printf 'FALLBACK_RUN=%s REF=%s\n' "$*" "$SARONIC_REF"
RUN
touch "$fixture/source/compose.yaml" "$fixture/source/compose.stream.yaml"
echo 'output/' > "$fixture/source/.gitignore"
git -C "$fixture/source" add .
git -C "$fixture/source" commit --quiet -m 'Starter without startup entry point'
git -C "$fixture/source" switch --quiet tanush/test-feature
mkdir -p "$fixture/bin"
cat > "$fixture/bin/docker" <<'DOCKER'
#!/bin/bash
printf 'FALLBACK_DOCKER=%s HOST=%s\n' "$*" "$ISAACSIM_HOST"
DOCKER
chmod +x "$fixture/bin/docker"
export PATH="$fixture/bin:$PATH" ISAACSIM_HOST=127.0.0.1
git clone --quiet "$fixture/source" "$fixture/checkout"
export SARONIC_REPO_DIR="$fixture/checkout" BOAT_X=12
unset SARONIC_REF
output="$(bash "$bootstrap_script" 2>&1)"
[[ "$output" == *"STARTUP=main REF=main SHA=$main_sha BOAT_X=12"* ]]
output="$(SARONIC_REF=tanush/test-feature bash "$bootstrap_script" 2>&1)"
[[ "$output" == *"STARTUP=feature REF=tanush/test-feature SHA=$feature_sha BOAT_X=12"* ]]
output="$(SARONIC_REF=refs/tags/test-release bash "$bootstrap_script" 2>&1)"
[[ "$output" == *"STARTUP=main REF=refs/tags/test-release SHA=$main_sha"* ]]
output="$(SARONIC_REF=no-startup bash "$bootstrap_script" 2>&1)"
[[ "$output" == *'Repo startup script missing; using temporary starter fallback.'* ]]
[[ "$output" == *'FALLBACK_RUN=deploy boat --x 12 --y 0 REF=no-startup'* ]]
[[ "$output" == *'FALLBACK_DOCKER=compose -f compose.yaml -f compose.stream.yaml up --build --force-recreate -d HOST=127.0.0.1'* ]]
[[ "$(git -C "$fixture/checkout" rev-parse HEAD)" == "$(git -C "$fixture/source" rev-parse no-startup)" ]]
for bad_ref in missing-branch '--upload-pack=bad' '../bad'; do
  if output="$(SARONIC_REF="$bad_ref" bash "$bootstrap_script" 2>&1)"; then
    echo "Unexpected success for $bad_ref" >&2; exit 1
  fi
  [[ "$output" != *'STARTUP='* ]]
done
printf '\n# local edit\n' >> "$fixture/checkout/scripts/brev-setup.sh"
if output="$(SARONIC_REF=main bash "$bootstrap_script" 2>&1)"; then
  echo 'Unexpected success for dirty checkout' >&2; exit 1
fi
[[ "$output" == *'Checkout has local changes'* ]]
echo 'PASS: default main, feature branch, tag, env propagation, invalid refs, local-change protection, and missing-script fallback'
