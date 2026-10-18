#!/usr/bin/env bash
# Startup reporting against a stub backend and a fake docker, without a GPU.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$(mktemp -d)"
server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill "$server_pid" 2>/dev/null
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$fixture"
}
trap cleanup EXIT

# Stub backend: records each request as one JSON line; rejects a wrong token.
cat > "$fixture/backend.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
record = open(sys.argv[1], "a")
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ok = self.headers.get("X-Ingest-Token") == "test-token"
        record.write(json.dumps({"path": self.path, "body": body, "ok": ok}) + "\n")
        record.flush()
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *args):
        pass
server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
PY
python3 -I "$fixture/backend.py" "$fixture/requests.jsonl" > "$fixture/port" &
server_pid=$!
for _ in $(seq 50); do [[ -s "$fixture/port" ]] && break; sleep 0.1; done

# Fake docker: `compose logs` prints sim.log, `compose ps -q` an id, `inspect` the health file.
mkdir "$fixture/bin"
cat > "$fixture/bin/docker" <<SH
#!/usr/bin/env bash
case "\$*" in
  *" logs "*"--tail=400"*) tail -n 400 "$fixture/sim.log" ;;
  *" logs "*) cat "$fixture/sim.log" ;;
  *" ps -q sim") echo fake-container ;;
  inspect*) cat "$fixture/health" ;;
esac
SH
chmod +x "$fixture/bin/docker"

export PATH="$fixture/bin:$PATH"
export SARONIC_BACKEND_URL="http://127.0.0.1:$(cat "$fixture/port")"
export SARONIC_SESSION_ID=0b6a3f3e-0000-4000-8000-000000000001
export SARONIC_INGEST_TOKEN=test-token

requests() {
  python3 -I - "$fixture/requests.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    request = json.loads(line)
    body = request["body"]
    parts = [request["path"].rsplit("/", 1)[1], body.get("step") or "log:" + body["source"]]
    parts += [f"{k}={body[k]}" for k in ("failed_step", "error_code") if k in body]
    parts += ["tail"] if body.get("log_tail") or body.get("body") else []
    parts += [] if request["ok"] else ["REJECTED"]
    print(" ".join(parts))
PY
}
expect() {
  local actual
  actual="$(requests)"
  if [[ "$actual" != "$1" ]]; then
    printf 'FAIL: %s\nexpected:\n%s\nactual:\n%s\n' "$2" "$1" "$actual" >&2
    exit 1
  fi
  : > "$fixture/requests.jsonl"
}

# Every stage marker present and healthy: steps in order, launch log, then ready.
# The markers sit before 500 lines of Isaac Sim output, beyond the failure-tail window.
{
  printf '%s\n' 'HarborSceneLoaded: built' 'HarborPhysicsReady: attached' 'HarborCamerasReady: Forward, Mast rendered'
  for i in $(seq 500); do echo "[omni.kit] noise $i"; done
} > "$fixture/sim.log"
echo healthy > "$fixture/health"
bash "$repo_dir/scripts/report-readiness.sh"
expect "$(printf '%s\n' 'events scene_loaded' 'events physics_running' 'events cameras_ready' 'logs log:isaac_launch tail' 'events ready')" "healthy startup"

# Physics never initialized and the container went unhealthy.
printf '%s\n' 'HarborSceneLoaded: built' 'Traceback: PhysX failed' > "$fixture/sim.log"
echo unhealthy > "$fixture/health"
if bash "$repo_dir/scripts/report-readiness.sh"; then echo 'FAIL: unhealthy startup exited 0' >&2; exit 1; fi
expect "$(printf '%s\n' 'events scene_loaded' 'logs log:isaac_launch tail' 'events failed failed_step=physics_running error_code=SIM_UNHEALTHY tail')" "unhealthy startup"

# Nothing within the timeout.
: > "$fixture/sim.log"
echo starting > "$fixture/health"
if SARONIC_READY_TIMEOUT=0 bash "$repo_dir/scripts/report-readiness.sh"; then echo 'FAIL: timeout exited 0' >&2; exit 1; fi
expect "$(printf '%s\n' 'logs log:isaac_launch' 'events failed failed_step=scene_loaded error_code=STARTUP_TIMEOUT')" "startup timeout"

# Wrong token is reported as rejected and not retried; unset variables send nothing.
SARONIC_INGEST_TOKEN=wrong python3 -I "$repo_dir/scripts/report-step.py" instance_up </dev/null 2>/dev/null
SARONIC_BACKEND_URL= python3 -I "$repo_dir/scripts/report-step.py" instance_up </dev/null
expect 'events instance_up REJECTED' "token and unconfigured reporting"

# An unreachable backend never fails the caller.
SARONIC_BACKEND_URL=http://127.0.0.1:1 python3 -I "$repo_dir/scripts/report-step.py" instance_up </dev/null 2>/dev/null

echo "PASS: ordered stage reports, ready, unhealthy and timeout failures with log tails, token rejection, unconfigured and unreachable backends"
