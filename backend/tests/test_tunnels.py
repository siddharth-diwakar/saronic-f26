"""Bridge tunnels: supervision, recovery, credential delivery, and worker registration."""

import itertools
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

import pytest
from fakes import FakeBrev, FakeClock, InMemoryStore

from saronic_api.brev import BrevError
from saronic_api.bridge.client import BridgeHealth, BridgeUnavailable
from saronic_api.bridge.tunnel import (
    BACKOFF_MAX_S,
    REFRESH_MIN_INTERVAL_S,
    BridgeReport,
    BridgeTunnels,
    CliSsh,
    TunnelError,
    TunnelTarget,
    advertise_host_for,
    session_payload,
)
from saronic_api.sessions.service import apply_bridge_report, clear_bridge
from saronic_api.worker import Worker

SESSION = uuid.UUID("12345678-1234-5678-1234-567812345678")
TARGET = TunnelTarget(SESSION, "saronic-123456781234", "token-1")
ALIAS = "saronic-123456781234-host"
PORTS = range(18200, 18204)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.tail = ""
        self.closed = False

    def poll(self):
        return self.returncode

    def error_tail(self):
        return self.tail

    def close(self):
        self.closed = True

    def exit(self, code=255, tail="Connection reset by peer"):
        self.returncode, self.tail = code, tail


class FakeSsh:
    def __init__(self):
        self.calls = []
        self.processes = []
        self.push_error = None
        self.files = {}  # alias -> payload on the "instance"

    def push(self, alias, remote_path, payload):
        self.calls.append(("push", alias, remote_path))
        if self.push_error:
            raise TunnelError(self.push_error)
        self.files[alias] = payload

    def forward(self, alias, bind_host, local_port, remote_port):
        self.calls.append(("forward", alias, bind_host, local_port, remote_port))
        process = FakeProcess()
        self.processes.append(process)
        return process

    def kinds(self):
        return [call[0] for call in self.calls]


class FakeBridge:
    """Answers the probe like the simulator bridge would for the session file it holds."""

    def __init__(self, ssh):
        self.ssh = ssh
        self.runtime_id = "runtime-a"
        self.ready = True
        self.down = False  # tunnel up, nothing listening on the instance
        self.serves_session = None  # override which session the bridge reports
        self.probes = []

    def __call__(self, host, port, token):
        self.probes.append((host, port, token))
        if self.down:
            raise BridgeUnavailable("Bridge unreachable", code="bridge_unreachable")
        payload = self.ssh.files.get(ALIAS)
        if payload is None:
            return BridgeHealth(self.runtime_id, None, self.ready)  # open, not provisioned
        if payload != session_payload(SESSION, token):
            raise BridgeUnavailable("rejected", code="bridge_unauthorized")
        return BridgeHealth(self.runtime_id, self.serves_session or str(SESSION), self.ready)


class Monotonic:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(**overrides):
    ssh = FakeSsh()
    bridge = FakeBridge(ssh)
    clock = Monotonic()
    refreshes = []
    options = {
        "ssh": ssh,
        "refresh": lambda: refreshes.append(clock.now),
        "advertise_host": "10.0.0.5",
        "ports": PORTS,
        "prober": bridge,
        "port_free": lambda host, port: True,
        "listening": lambda host, port: True,
        "clock": clock,
        "sleep": clock.sleep,
        **overrides,
    }
    return BridgeTunnels(**options), ssh, bridge, clock, refreshes


def test_first_connect_refreshes_pushes_then_forwards_and_reports_ready():
    tunnels, ssh, bridge, _, refreshes = make()
    reports, closed = tunnels.sync([TARGET])
    assert len(refreshes) == 1
    assert ssh.calls == [
        ("push", ALIAS, "saronic-f26/output/.bridge-session.json"),
        ("forward", ALIAS, "0.0.0.0", 18200, 8212),
    ]
    assert bridge.probes == [("127.0.0.1", 18200, "token-1")]
    assert reports == {SESSION: BridgeReport("10.0.0.5", 18200, "runtime-a", True, None)}
    assert closed == []

    # Steady state: probe only.
    tunnels.sync([TARGET])
    assert ssh.kinds() == ["push", "forward"] and len(refreshes) == 1


def test_loading_and_silent_bridge_keep_the_tunnel_up():
    tunnels, ssh, bridge, _, _ = make()
    bridge.ready = False
    report = tunnels.sync([TARGET])[0][SESSION]
    assert (report.port, report.ready, report.error) == (18200, False, "Simulator is loading")

    bridge.down = True
    report = tunnels.sync([TARGET])[0][SESSION]
    assert (report.host, report.port, report.runtime_id, report.ready) == (
        "10.0.0.5",
        18200,
        None,
        False,
    )
    assert "not answering" in report.error
    assert ssh.kinds() == ["push", "forward"]  # no reconnect while ssh itself is fine


def test_exited_tunnel_reports_down_then_reconnects_after_backoff():
    tunnels, ssh, _, clock, refreshes = make()
    tunnels.sync([TARGET])
    ssh.processes[0].exit(tail="client_loop: send disconnect: Broken pipe")

    report = tunnels.sync([TARGET])[0][SESSION]
    assert report == BridgeReport(
        None, None, None, False, "SSH tunnel exited: client_loop: send disconnect: Broken pipe"
    )
    assert ssh.processes[0].closed
    tunnels.sync([TARGET])  # still backing off
    assert ssh.kinds() == ["push", "forward"]

    clock.now += 5
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.ready and report.port == 18200
    # The session file is already on the instance; refresh waits for its minimum interval.
    assert ssh.kinds() == ["push", "forward", "forward"] and len(refreshes) == 1


def dying_forward(ssh, tail="ssh: connect to host 1.2.3.4 port 22: Connection timed out"):
    original = ssh.forward

    def forward(*args):
        process = original(*args)
        process.exit(tail=tail)
        return process

    ssh.forward = forward


def test_repeated_failures_back_off_and_refresh_ssh_config():
    tunnels, ssh, _, clock, refreshes = make(listening=lambda host, port: False)
    dying_forward(ssh)
    delays = []
    for _ in range(6):
        report = tunnels.sync([TARGET])[0][SESSION]
        assert report.port is None and "Connection timed out" in report.error
        retry_at = tunnels._tunnels[SESSION].retry_at
        delays.append(retry_at - clock.now)
        clock.now = retry_at
    assert delays == [5, 10, 20, 40, BACKOFF_MAX_S, BACKOFF_MAX_S]
    # Failures refresh Brev's SSH config again (new IP or cert), at most once a minute.
    assert len(refreshes) >= 3
    assert all(b - a >= REFRESH_MIN_INTERVAL_S for a, b in itertools.pairwise(refreshes))
    assert ssh.kinds().count("push") == 1  # the session file is written once


def test_success_resets_backoff():
    tunnels, ssh, _, clock, _ = make()
    tunnels.sync([TARGET])
    ssh.processes[-1].exit()
    tunnels.sync([TARGET])
    clock.now += 5
    tunnels.sync([TARGET])
    assert tunnels._tunnels[SESSION].failures == 0


def test_ssh_that_exits_while_connecting_is_a_failure():
    tunnels, ssh, _, _, _ = make(listening=lambda host, port: False)
    dying_forward(ssh, tail="Permission denied (publickey)")
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.port is None
    assert report.error == "SSH tunnel exited: Permission denied (publickey)"
    assert ssh.processes[0].closed


def test_slow_connect_is_reported_and_retried_without_a_new_process():
    tunnels, ssh, bridge, clock, _ = make(listening=lambda host, port: False)
    bridge.down = True
    started = clock.now
    report = tunnels.sync([TARGET])[0][SESSION]
    assert clock.now - started >= 15  # waited CONNECT_WAIT_S
    assert report.port == 18200 and not report.ready
    tunnels.sync([TARGET])
    assert ssh.kinds() == ["push", "forward"]


def test_missing_output_directory_blocks_the_tunnel():
    tunnels, ssh, _, clock, _ = make()
    ssh.push_error = "Could not write the session file on x: output does not exist yet"
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.port is None and "does not exist yet" in report.error
    assert ssh.kinds() == ["push"]  # never forwards an unprovisioned bridge

    ssh.push_error = None
    clock.now += 5
    assert tunnels.sync([TARGET])[0][SESSION].ready
    assert ssh.kinds() == ["push", "push", "forward"]


def test_refresh_failure_is_reported():
    def failing():
        raise BrevError("brev refresh exited with 1: unauthorized")

    tunnels, ssh, _, _, _ = make(refresh=failing)
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.error == "brev refresh failed: brev refresh exited with 1: unauthorized"
    assert ssh.calls == []


def test_bridge_without_the_session_file_gets_it_again():
    tunnels, ssh, _, clock, _ = make()
    tunnels.sync([TARGET])
    del ssh.files[ALIAS]  # e.g. output/ was wiped on the instance
    report = tunnels.sync([TARGET])[0][SESSION]
    assert (report.port, report.ready) == (18200, False)
    assert "has not loaded the session file" in report.error
    assert ssh.kinds() == ["push", "forward", "push"]
    clock.now += 5
    assert tunnels.sync([TARGET])[0][SESSION].ready


def test_rejected_token_is_rewritten():
    tunnels, ssh, _, clock, _ = make()
    tunnels.sync([TARGET])
    ssh.files[ALIAS] = session_payload(SESSION, "someone-else")
    report = tunnels.sync([TARGET])[0][SESSION]
    assert not report.ready and "does not hold this session's token" in report.error
    clock.now += 5
    assert tunnels.sync([TARGET])[0][SESSION].ready


def test_tunnel_to_another_sessions_simulator_is_dropped():
    tunnels, ssh, bridge, _, _ = make()
    bridge.serves_session = str(uuid.uuid4())
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.port is None and "instead" in report.error
    assert ssh.processes[0].closed


def test_restarted_simulator_reports_its_new_runtime():
    tunnels, _, bridge, _, _ = make()
    assert tunnels.sync([TARGET])[0][SESSION].runtime_id == "runtime-a"
    bridge.runtime_id = "runtime-b"
    assert tunnels.sync([TARGET])[0][SESSION].runtime_id == "runtime-b"


def test_ports_skip_busy_ones_and_sessions_get_distinct_ports():
    other = TunnelTarget(uuid.uuid4(), "saronic-abcdefabcdef", "token-2")
    tunnels, ssh, _, _, _ = make(port_free=lambda host, port: port != 18200)
    tunnels.sync([TARGET, other])
    assert [call[3] for call in ssh.calls if call[0] == "forward"] == [18201, 18202]


def test_no_free_port_is_reported():
    tunnels, _, _, _, _ = make(port_free=lambda host, port: False)
    assert "No free tunnel port" in tunnels.sync([TARGET])[0][SESSION].error


def test_sessions_that_stop_running_lose_their_tunnel():
    tunnels, ssh, _, _, _ = make()
    tunnels.sync([TARGET])
    reports, closed = tunnels.sync([])
    assert reports == {} and closed == [SESSION]
    assert ssh.processes[0].closed


def test_close_all_stops_every_process():
    tunnels, ssh, _, _, _ = make()
    tunnels.sync([TARGET, TunnelTarget(uuid.uuid4(), "saronic-abcdefabcdef", "t")])
    tunnels.close_all()
    assert all(process.closed for process in ssh.processes)


def test_unexpected_error_is_contained_per_session():
    def broken(host, port, token):
        raise RuntimeError("boom")

    tunnels, ssh, _, _, _ = make(prober=broken)
    report = tunnels.sync([TARGET])[0][SESSION]
    assert report.port is None and report.error == "Internal error"
    assert ssh.processes[0].closed


@pytest.mark.parametrize(
    ("bind", "configured", "expected"),
    [
        ("0.0.0.0", "worker.internal", "worker.internal"),
        ("127.0.0.1", None, "127.0.0.1"),
        ("10.1.2.3", None, "10.1.2.3"),
    ],
)
def test_advertise_host(bind, configured, expected):
    assert advertise_host_for(bind, configured) == expected


def test_advertise_host_falls_back_when_hostname_does_not_resolve(monkeypatch):
    def unresolvable(name):
        raise OSError("no address")

    monkeypatch.setattr("socket.gethostbyname", unresolvable)
    assert advertise_host_for("0.0.0.0", None) == "127.0.0.1"


# --- Registration on the session row ---


def test_report_moves_connected_at_only_for_a_new_runtime(store_record):
    first = datetime(2026, 11, 15, 12, tzinfo=UTC)
    later = datetime(2026, 11, 15, 12, 5, tzinfo=UTC)
    apply_bridge_report(store_record, BridgeReport("h", 1, "runtime-a", True, None), first)
    apply_bridge_report(store_record, BridgeReport("h", 1, "runtime-a", True, None), later)
    assert (store_record.bridge_connected_at, store_record.bridge_checked_at) == (first, later)

    # Bridge silent: keep the last verified runtime, but not ready.
    apply_bridge_report(store_record, BridgeReport("h", 1, None, False, "not answering"), later)
    assert store_record.bridge_runtime_id == "runtime-a" and not store_record.bridge_ready
    assert store_record.bridge_error == "not answering"

    apply_bridge_report(store_record, BridgeReport("h", 1, "runtime-b", True, None), later)
    assert store_record.bridge_runtime_id == "runtime-b"
    assert store_record.bridge_connected_at == later
    assert store_record.bridge_error is None
    assert store_record.last_activity_at is None  # bridge checks are not session activity


def test_report_ignored_once_the_session_stopped(store_record):
    store_record.status = "stopping"
    apply_bridge_report(
        store_record, BridgeReport("h", 1, "runtime-a", True, None), datetime.now(UTC)
    )
    assert store_record.bridge_port is None and not store_record.bridge_ready


def test_ready_needs_a_tunnel(store_record):
    apply_bridge_report(store_record, BridgeReport(None, None, "r", True, None), datetime.now(UTC))
    assert not store_record.bridge_ready


@pytest.fixture
def store_record():
    return InMemoryStore().add(status="running")


# --- Worker ---


def test_worker_registers_running_sessions_and_clears_stopped_ones():
    store, brev, clock = InMemoryStore(), FakeBrev(), FakeClock()
    tunnels, ssh, _, _, _ = make()
    worker = Worker(store, brev, launchable_id="env-test", clock=clock, tunnels=tunnels)
    record = store.add()
    other = store.add()

    worker.tick()  # provisioning: no tunnel before Brev reports RUNNING
    assert ssh.calls == [] and record.bridge_port is None

    brev.set(record.instance_name, "RUNNING", "COMPLETED")
    alias = record.instance_name + "-host"
    bridge_session = str(record.id)
    tunnels.prober = lambda host, port, token: BridgeHealth("runtime-a", bridge_session, True)
    clock.advance(10)
    worker.tick()
    assert record.status == "running"
    assert (record.bridge_host, record.bridge_port) == ("10.0.0.5", 18200)
    assert (record.bridge_runtime_id, record.bridge_ready) == ("runtime-a", True)
    assert record.bridge_connected_at == clock.now == record.bridge_checked_at
    assert ssh.calls[0] == ("push", alias, "saronic-f26/output/.bridge-session.json")
    assert other.bridge_port is None  # still provisioning

    with store.locked(record.id) as locked:
        locked.status = "stopping"
    clock.advance(10)
    worker.tick()
    assert ssh.processes[0].closed
    assert (record.bridge_port, record.bridge_ready) == (None, False)
    assert record.bridge_runtime_id == "runtime-a"  # kept as history


def test_worker_without_tunnels_does_not_touch_bridge_fields():
    store, brev = InMemoryStore(), FakeBrev()
    worker = Worker(store, brev, launchable_id="env-test", clock=FakeClock())
    record = store.add(status="running")
    brev.set(record.instance_name)
    worker.tick()
    assert record.bridge_checked_at is None


def test_clear_bridge_keeps_runtime_history(store_record):
    store_record.bridge_host, store_record.bridge_port = "h", 1
    store_record.bridge_runtime_id, store_record.bridge_ready = "r", True
    clear_bridge(store_record, datetime.now(UTC))
    assert (store_record.bridge_host, store_record.bridge_port) == (None, None)
    assert store_record.bridge_runtime_id == "r" and not store_record.bridge_ready


# --- CliSsh against a fake ssh executable ---


FAKE_SSH = """#!{python}
import json, os, sys, time
log = os.environ["FAKE_SSH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": "" if "-N" in sys.argv else sys.stdin.read(),
                         "key": os.environ.get("BREV_API_KEY")}}) + "\\n")
mode = os.environ.get("FAKE_SSH_MODE", "ok")
if mode == "fail":
    sys.stderr.write("Warning: something\\nbrev_secret_key: Permission denied (publickey)\\n")
    sys.exit(255)
if "-N" in sys.argv:
    time.sleep(60)
"""


@pytest.fixture
def fake_ssh(tmp_path):
    path = tmp_path / "ssh"
    path.write_text(FAKE_SSH.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log.jsonl"
    env = {"PATH": os.environ["PATH"], "FAKE_SSH_LOG": str(log), "BREV_API_KEY": "brev_secret_key"}
    return path, log, env


def logged(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_cli_push_sends_payload_on_stdin_with_unattended_options(fake_ssh):
    path, log, env = fake_ssh
    CliSsh(env=env, ssh_path=str(path), secret="brev_secret_key").push(
        ALIAS, "saronic-f26/output/.bridge-session.json", b'{"session_id": "s"}'
    )
    (call,) = logged(log)
    argv = call["argv"]
    for option in ("BatchMode=yes", "ControlPath=none", "ForwardAgent=no", "RemoteCommand=none"):
        assert option in argv
    assert argv[-2] == ALIAS and argv[-1].startswith("sh -c ")
    assert call["stdin"] == '{"session_id": "s"}'
    assert call["key"] == "brev_secret_key"  # for Brev's `Match exec brev mint-cert`
    assert "brev_secret_key" not in " ".join(argv)


def test_cli_push_failure_is_redacted(fake_ssh):
    path, _, env = fake_ssh
    ssh = CliSsh(env={**env, "FAKE_SSH_MODE": "fail"}, ssh_path=str(path), secret="brev_secret_key")
    with pytest.raises(TunnelError) as caught:
        ssh.push(ALIAS, "f", b"x")
    assert "Permission denied" in str(caught.value)
    assert "brev_secret_key" not in str(caught.value)


def test_cli_forward_runs_until_closed(fake_ssh):
    path, log, env = fake_ssh
    process = CliSsh(env=env, ssh_path=str(path)).forward(ALIAS, "0.0.0.0", 18200, 8212)
    try:
        deadline = time.monotonic() + 5
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() is None
        argv = logged(log)[0]["argv"]
        assert "-N" in argv and "ExitOnForwardFailure=yes" in argv
        assert argv[argv.index("-L") + 1] == "0.0.0.0:18200:127.0.0.1:8212"
        assert argv[-1] == ALIAS
    finally:
        process.close()
    assert process.poll() is not None


def test_cli_forward_exit_keeps_stderr_tail(fake_ssh):
    path, _, env = fake_ssh
    process = CliSsh(
        env={**env, "FAKE_SSH_MODE": "fail"}, ssh_path=str(path), secret="brev_secret_key"
    ).forward(ALIAS, "0.0.0.0", 18200, 8212)
    try:
        process._process.wait(timeout=5)
        assert process.poll() == 255
        assert (
            process.error_tail() == "Warning: something | [REDACTED]: Permission denied (publickey)"
        )
    finally:
        process.close()


def test_missing_ssh_binary_is_a_tunnel_error(tmp_path):
    ssh = CliSsh(env={}, ssh_path=str(tmp_path / "no-ssh"))
    with pytest.raises(TunnelError):
        ssh.push(ALIAS, "f", b"x")
    with pytest.raises(TunnelError):
        ssh.forward(ALIAS, "127.0.0.1", 18200, 8212)


def test_brev_refresh_command(monkeypatch):
    from saronic_api.brev import CliBrevClient

    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs["env"]["BREV_API_KEY"]))
        return subprocess.CompletedProcess(argv, 1, "", "error: brev_key_123 rejected")

    brev = CliBrevClient(api_key="brev_key_123", cli_path="brev", runner=runner)
    with pytest.raises(BrevError) as caught:
        brev.refresh_ssh_config()
    assert calls == [(["brev", "refresh"], "brev_key_123")]
    assert "brev_key_123" not in str(caught.value)
