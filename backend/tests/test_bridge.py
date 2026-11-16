"""Backend bridge client against the real simulator bridge (src/saronic_sim), without Isaac Sim.

These pin the contract between saronic_api.bridge and src/saronic_sim/access.py: the
session file the worker writes, the token header, runtime IDs, and error mapping.
"""

import os
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from saronic_api.bridge import (
    BridgeClient,
    BridgeRuntimeChanged,
    BridgeUnavailable,
    bridge_connection,
)
from saronic_api.bridge.tunnel import push_script, session_payload

SIM_DIR = Path(__file__).resolve().parents[2] / "src" / "saronic_sim"
sys.path.append(str(SIM_DIR))  # appended, so it cannot shadow installed modules
from access import Access
from commands import CommandQueue, start_server

TOKEN = "per-session-token"


@pytest.fixture
def bridge(tmp_path):
    """A guarded bridge with no session file yet, like a freshly started simulator."""
    commands = CommandQueue()
    access = Access(str(tmp_path / ".bridge-session.json"), runtime_id="runtime-a")
    server = start_server(commands, ["Forward"], port=0, access=access)
    yield server.server_address[1], commands, tmp_path / ".bridge-session.json"
    server.shutdown()
    server.server_close()


def provision(path: Path, session_id: uuid.UUID, token: str = TOKEN) -> None:
    """What CliSsh.push does on the instance: write by rename."""
    path.with_suffix(".tmp").write_bytes(session_payload(session_id, token))
    os.replace(path.with_suffix(".tmp"), path)


def test_worker_session_file_is_accepted_by_the_bridge(bridge):
    port, commands, path = bridge
    session_id = uuid.uuid4()
    health = BridgeClient("127.0.0.1", port, TOKEN).health()
    assert (health.runtime_id, health.session_id) == ("runtime-a", None)  # open until provisioned

    provision(path, session_id)
    health = BridgeClient("127.0.0.1", port, TOKEN).health()
    assert (health.session_id, health.ready) == (str(session_id), False)
    commands.publish({"time": 0.0})
    assert BridgeClient("127.0.0.1", port, TOKEN).health().ready is True
    assert TOKEN not in path.read_text()  # only the hash is on the instance


def test_wrong_token_and_lost_session_file_are_unavailable(bridge):
    port, _, path = bridge
    provision(path, uuid.uuid4())
    with pytest.raises(BridgeUnavailable) as wrong:
        BridgeClient("127.0.0.1", port, "someone-else").health()
    assert wrong.value.code == "bridge_unauthorized"
    path.unlink()
    with pytest.raises(BridgeUnavailable) as lost:
        BridgeClient("127.0.0.1", port, TOKEN).health()
    assert lost.value.code == "bridge_session_unavailable"


def test_commands_are_pinned_to_a_runtime(bridge):
    port, commands, path = bridge
    provision(path, uuid.uuid4())
    current = BridgeClient("127.0.0.1", port, TOKEN, runtime_id="runtime-a")
    response = current.request("POST", "/commands", {"type": "reset"})
    assert (response.status, response.body["status"], response.runtime_id) == (
        202,
        "queued",
        "runtime-a",
    )
    # Bridge validation errors come back as responses, not exceptions.
    assert current.request("POST", "/commands", {"type": "explode"}).status == 400

    stale = BridgeClient("127.0.0.1", port, TOKEN, runtime_id="runtime-before-restart")
    with pytest.raises(BridgeRuntimeChanged) as changed:
        stale.request("GET", f"/commands/{response.body['id']}")
    assert changed.value.runtime_id == "runtime-a"
    assert [command for _, command in commands.drain()] == [{"type": "reset"}]


def test_nothing_listening_is_unreachable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(BridgeUnavailable) as caught:
        BridgeClient("127.0.0.1", port, TOKEN, timeout_s=1).health()
    assert caught.value.code == "bridge_unreachable"


def test_tunnel_up_but_bridge_down_is_unreachable():
    """ssh -L accepts the connection, then closes it when the instance's port refuses."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def accept_and_close():
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.close()

    thread = threading.Thread(target=accept_and_close, daemon=True)
    thread.start()
    try:
        with pytest.raises(BridgeUnavailable) as caught:
            BridgeClient("127.0.0.1", listener.getsockname()[1], TOKEN, timeout_s=2).health()
    finally:
        thread.join(2)
        listener.close()
    assert caught.value.code == "bridge_unreachable"


# --- Remote push script, run locally with sh ---


def run_push(home: Path, remote_path: str, payload: bytes) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run(
        ["sh", "-c", push_script(remote_path)],
        input=payload,
        capture_output=True,
        env=env,
        check=False,
    )


def test_push_script_writes_under_home_by_rename(tmp_path):
    output = tmp_path / "saronic-f26" / "output"
    output.mkdir(parents=True)
    result = run_push(tmp_path, "saronic-f26/output/.bridge-session.json", b'{"a": 1}')
    assert result.returncode == 0, result.stderr
    assert (output / ".bridge-session.json").read_bytes() == b'{"a": 1}'
    assert not (output / ".bridge-session.json.tmp").exists()


def test_push_script_handles_absolute_paths_with_spaces(tmp_path):
    target = tmp_path / "dir with space" / "session.json"
    target.parent.mkdir()
    assert run_push(tmp_path, str(target), b"x").returncode == 0
    assert target.read_bytes() == b"x"


def test_push_script_waits_for_the_output_directory(tmp_path):
    result = run_push(tmp_path, "saronic-f26/output/.bridge-session.json", b"x")
    assert result.returncode == 3
    assert b"does not exist yet" in result.stderr


# --- Registration the API reads ---


class Row:
    def __init__(self, **fields):
        self.id = uuid.uuid4()
        self.status = "running"
        self.bridge_token = TOKEN
        self.bridge_host = "10.0.0.5"
        self.bridge_port = 18200
        self.bridge_runtime_id = "runtime-a"
        self.bridge_ready = True
        self.bridge_error = None
        self.__dict__.update(fields)


def test_bridge_connection_for_a_ready_session():
    row = Row()
    connection = bridge_connection(row)
    assert (connection.host, connection.port, connection.token, connection.runtime_id) == (
        "10.0.0.5",
        18200,
        TOKEN,
        "runtime-a",
    )


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"status": "stopping"}, "session_not_running"),
        ({"bridge_host": None, "bridge_port": None, "bridge_error": "ssh: timeout"}, None),
        ({"bridge_ready": False}, "bridge_not_ready"),
        ({"bridge_runtime_id": None}, "bridge_not_ready"),
    ],
)
def test_bridge_connection_explains_why_it_is_unavailable(fields, code):
    with pytest.raises(BridgeUnavailable) as caught:
        bridge_connection(Row(**fields))
    if code is None:
        assert caught.value.code == "bridge_not_connected"
        assert "ssh: timeout" in str(caught.value)
    else:
        assert caught.value.code == code
