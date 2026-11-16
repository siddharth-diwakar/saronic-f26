"""SSH tunnels from the worker to each running session's simulator bridge.

For every running session the worker, on each tick:

1. Writes the session file on the instance over SSH ({"session_id", "token_sha256"}), so the
   bridge accepts this session's token. This happens before any tunnel exists.
2. Keeps one `ssh -N -L <bind>:<port>:127.0.0.1:<bridge port> <instance>-host` running. The
   `-host` alias is the Brev VM itself; the sim container uses host networking, so the
   VM's 127.0.0.1 is the simulator's. A Brev workspace container (`<instance>`) would not
   reach it.
3. Calls GET /health through the tunnel and checks the bridge reports this session's ID.
   That proves the tunnel reached this session's simulator, not some other process.

A tunnel that exits is restarted with backoff, after `brev refresh` (new IP or SSH cert).
`brev port-forward` is not used: it starts ssh in the background and exits, leaving a
process nothing supervises. Brev's SSH config enables ControlPersist, ForwardAgent and a TTY;
the options below override them for an unattended forward.
"""

import hashlib
import json
import logging
import shlex
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import IO, Protocol

from saronic_api.brev import BrevError
from saronic_api.bridge.client import BridgeClient, BridgeHealth, BridgeUnavailable

logger = logging.getLogger(__name__)

BACKOFF_BASE_S = 5.0
BACKOFF_MAX_S = 60.0
# Refresh Brev's SSH config at most this often after failures (it can take seconds).
REFRESH_MIN_INTERVAL_S = 60.0
CONNECT_WAIT_S = 15.0
PUSH_TIMEOUT_S = 60.0
ERROR_MAX = 2000
_TAIL_BYTES = 4096
_TAIL_LINES = 6

# Unattended: never prompt, never multiplex or persist, no agent forwarding, no TTY.
SSH_OPTIONS = (
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "ControlPersist=no",
    "-o", "ForwardAgent=no",
    "-o", "RequestTTY=no",
    "-o", "RemoteCommand=none",
    "-o", "LogLevel=ERROR",
)  # fmt: skip
FORWARD_OPTIONS = (
    "-N",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
)  # fmt: skip


class TunnelError(Exception):
    """Could not set up or keep a tunnel. Messages never contain the Brev key."""


@dataclass(frozen=True)
class TunnelTarget:
    """A running session the worker should keep a tunnel to."""

    session_id: uuid.UUID
    instance_name: str
    token: str


@dataclass(frozen=True)
class BridgeReport:
    """What the worker records on the session after a connect pass.

    host/port are None while no tunnel is up. runtime_id is None when the bridge did not
    answer this pass; the session keeps its last verified runtime then.
    """

    host: str | None
    port: int | None
    runtime_id: str | None
    ready: bool
    error: str | None


class TunnelProcess(Protocol):
    def poll(self) -> int | None: ...

    def error_tail(self) -> str: ...

    def close(self) -> None: ...


class SshRunner(Protocol):
    def push(self, alias: str, remote_path: str, payload: bytes) -> None:
        """Atomically write `payload` to `remote_path` on the instance. Raises TunnelError."""

    def forward(
        self, alias: str, bind_host: str, local_port: int, remote_port: int
    ) -> TunnelProcess:
        """Start `ssh -N -L`. Returns at once; the process may still be connecting."""


Prober = Callable[[str, int, str], BridgeHealth]


def session_payload(session_id: uuid.UUID, token: str) -> bytes:
    """The session file. Only the token's hash goes to the instance."""
    digest = hashlib.sha256(token.encode()).hexdigest()
    return json.dumps({"session_id": str(session_id), "token_sha256": digest}).encode()


def push_script(remote_path: str) -> str:
    """POSIX sh that writes stdin to `remote_path` by rename, so the bridge never reads half a file.

    A relative path is under the SSH user's home. Exits 3 when the directory does not exist yet:
    the instance's setup has not prepared the simulator's output directory.
    """
    target = shlex.quote(remote_path)
    path = target if remote_path.startswith("/") else f'"$HOME"/{target}'
    return (
        f'set -eu; f={path}; d=$(dirname "$f"); '
        'if [ ! -d "$d" ]; then echo "$d does not exist yet" >&2; exit 3; fi; '
        'cat > "$f.tmp"; mv -f "$f.tmp" "$f"'
    )


class CliSsh:
    """SshRunner using OpenSSH with the host entries `brev refresh` writes."""

    def __init__(
        self,
        *,
        env: Mapping[str, str],
        ssh_path: str = "ssh",
        push_timeout_s: float = PUSH_TIMEOUT_S,
        secret: str | None = None,
    ) -> None:
        # env carries BREV_API_KEY: Brev's SSH config may run `brev mint-cert` via Match exec.
        self._env = dict(env)
        self._ssh_path = ssh_path
        self._push_timeout_s = push_timeout_s
        self._secret = secret

    def push(self, alias: str, remote_path: str, payload: bytes) -> None:
        argv = [
            self._ssh_path,
            *SSH_OPTIONS,
            alias,
            f"sh -c {shlex.quote(push_script(remote_path))}",
        ]
        try:
            result = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                timeout=self._push_timeout_s,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise TunnelError(
                f"Writing the session file on {alias} timed out after {self._push_timeout_s}s"
            ) from None
        except OSError as exc:
            raise TunnelError(f"Could not run ssh at {self._ssh_path!r}: {exc.strerror}") from None
        if result.returncode != 0:
            detail = self._redact(_tail(result.stderr) or f"exit {result.returncode}")
            raise TunnelError(f"Could not write the session file on {alias}: {detail}")

    def forward(
        self, alias: str, bind_host: str, local_port: int, remote_port: int
    ) -> TunnelProcess:
        argv = [
            self._ssh_path,
            *SSH_OPTIONS,
            *FORWARD_OPTIONS,
            "-L",
            f"{bind_host}:{local_port}:127.0.0.1:{remote_port}",
            alias,
        ]
        # A file, not a pipe: nothing reads stderr until the process exits.
        stderr = tempfile.TemporaryFile()  # noqa: SIM115 - SshProcess.close() closes it
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=self._env,
            )
        except OSError as exc:
            stderr.close()
            raise TunnelError(f"Could not run ssh at {self._ssh_path!r}: {exc.strerror}") from None
        return SshProcess(process, stderr, redact=self._redact)

    def _redact(self, text: str) -> str:
        return text.replace(self._secret, "[REDACTED]") if self._secret else text


class SshProcess:
    def __init__(
        self,
        process: subprocess.Popen,
        stderr: IO[bytes],
        *,
        redact: Callable[[str], str] = lambda text: text,
    ) -> None:
        self._process = process
        self._stderr = stderr
        self._redact = redact

    def poll(self) -> int | None:
        return self._process.poll()

    def error_tail(self) -> str:
        try:
            self._stderr.seek(0, 2)
            self._stderr.seek(max(0, self._stderr.tell() - _TAIL_BYTES))
            return self._redact(_tail(self._stderr.read()))
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._stderr.close()


def _tail(data: bytes) -> str:
    lines = data.decode(errors="replace").strip().splitlines()
    return " | ".join(line.strip() for line in lines[-_TAIL_LINES:] if line.strip())


def port_is_free(host: str, port: int) -> bool:
    """Whether `ssh -L host:port` could bind. ssh sets SO_REUSEADDR, so this does too."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def advertise_host_for(bind_host: str, configured: str | None) -> str:
    """The address the API uses to reach the worker's forwarded ports."""
    if configured:
        return configured
    if bind_host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"
    if bind_host not in ("0.0.0.0", "", "::"):
        return bind_host
    # In a container, the hostname resolves to the container's IP on its network.
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        logger.warning(
            "Could not resolve this host's address; advertising 127.0.0.1. "
            "Set BRIDGE_TUNNEL_ADVERTISE_HOST if the API runs elsewhere."
        )
        return "127.0.0.1"


def probe_bridge(host: str, port: int, token: str, *, timeout_s: float = 5.0) -> BridgeHealth:
    return BridgeClient(host, port, token, timeout_s=timeout_s).health()


@dataclass
class _Tunnel:
    target: TunnelTarget
    process: TunnelProcess | None = None
    port: int | None = None
    pushed: bool = False  # session file written for this target
    failures: int = 0
    retry_at: float = 0.0
    error: str | None = None


@dataclass
class BridgeTunnels:
    """One supervised tunnel per running session. Not thread-safe; the worker loop owns it."""

    ssh: SshRunner
    refresh: Callable[[], None]  # `brev refresh`: writes ~/.brev/ssh_config and the SSH key
    advertise_host: str
    ports: Iterable[int]
    remote_port: int = 8212
    bind_host: str = "0.0.0.0"
    alias_suffix: str = "-host"
    remote_session_file: str = "saronic-f26/output/.bridge-session.json"
    prober: Prober = probe_bridge
    port_free: Callable[[str, int], bool] = port_is_free
    listening: Callable[[str, int], bool] = is_listening
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    beat: Callable[[], None] = lambda: None
    connect_wait_s: float = CONNECT_WAIT_S
    _tunnels: dict[uuid.UUID, _Tunnel] = field(default_factory=dict, init=False)
    _refreshed_at: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.ports = tuple(self.ports)

    @property
    def local_host(self) -> str:
        """Where the worker itself reaches its forwarded ports."""
        return "127.0.0.1" if self.bind_host in ("0.0.0.0", "", "::") else self.bind_host

    def sync(
        self, targets: Iterable[TunnelTarget]
    ) -> tuple[dict[uuid.UUID, BridgeReport], list[uuid.UUID]]:
        """Connect every target and close tunnels for sessions no longer listed.

        Returns a report per target and the IDs whose tunnels were closed.
        """
        wanted = {target.session_id: target for target in targets}
        closed = [session_id for session_id in self._tunnels if session_id not in wanted]
        for session_id in closed:
            self._close(self._tunnels.pop(session_id))
            logger.info("Closed bridge tunnel", extra={"session_id": str(session_id)})
        reports = {}
        for session_id, target in wanted.items():
            try:
                reports[session_id] = self._connect(target)
            except Exception:
                logger.exception("Bridge connect failed", extra={"session_id": str(session_id)})
                tunnel = self._tunnels.get(session_id)
                if tunnel is not None:
                    self._close(tunnel)
                    self._failed(tunnel, "Internal error while connecting to the bridge")
                reports[session_id] = BridgeReport(None, None, None, False, "Internal error")
        return reports, closed

    def close_all(self) -> None:
        for tunnel in self._tunnels.values():
            self._close(tunnel)
        self._tunnels.clear()

    def _connect(self, target: TunnelTarget) -> BridgeReport:
        tunnel = self._tunnels.setdefault(target.session_id, _Tunnel(target))
        if tunnel.target != target:
            # Same session, new instance name or token: start over.
            self._close(tunnel)
            tunnel.target, tunnel.pushed = target, False
        extra = {"session_id": str(target.session_id), "instance_name": target.instance_name}

        if tunnel.process is not None and tunnel.process.poll() is not None:
            detail = tunnel.process.error_tail() or "no output"
            self._close(tunnel)
            self._failed(tunnel, f"SSH tunnel exited: {detail}")
            logger.warning("Bridge tunnel exited: %s", detail, extra=extra)

        if tunnel.process is None:
            if self.clock() < tunnel.retry_at:
                return self._down(tunnel)
            try:
                self._establish(tunnel)
            except TunnelError as exc:
                self._close(tunnel)
                self._failed(tunnel, str(exc))
                logger.warning("Bridge tunnel not established: %s", exc, extra=extra)
                return self._down(tunnel)
            logger.info("Bridge tunnel up on port %s", tunnel.port, extra=extra)

        return self._probe(tunnel, extra)

    def _establish(self, tunnel: _Tunnel) -> None:
        alias = tunnel.target.instance_name + self.alias_suffix
        self._refresh_for(alias, after_failure=tunnel.failures > 0)
        if not tunnel.pushed:
            self._push(tunnel, alias)
        port = self._free_port()
        process = self.ssh.forward(alias, self.bind_host, port, self.remote_port)
        tunnel.process, tunnel.port = process, port
        deadline = self.clock() + self.connect_wait_s
        while not self.listening(self.local_host, port):
            if process.poll() is not None:
                detail = process.error_tail() or "no output"
                raise TunnelError(f"SSH tunnel exited: {detail}")
            if self.clock() >= deadline:
                # Still connecting; this pass's probe reports it and the next pass retries.
                return
            self.sleep(0.25)

    def _probe(self, tunnel: _Tunnel, extra: dict[str, str]) -> BridgeReport:
        target = tunnel.target
        try:
            health = self.prober(self.local_host, tunnel.port, target.token)
        except BridgeUnavailable as exc:
            if exc.code in ("bridge_unauthorized", "bridge_session_unavailable"):
                return self._repush(tunnel, f"Bridge does not hold this session's token ({exc})")
            # The tunnel is up; nothing answers on the bridge port yet.
            return self._up(tunnel, None, False, f"Simulator bridge not answering: {exc}")
        finally:
            self.beat()
        if health.session_id is None:
            return self._repush(
                tunnel,
                "Bridge has not loaded the session file; check BRIDGE_REMOTE_SESSION_FILE "
                f"({self.remote_session_file}) matches SARONIC_BRIDGE_SESSION_FILE",
            )
        if health.session_id != str(target.session_id):
            self._close(tunnel)
            self._failed(
                tunnel, f"Tunnel reached the simulator of session {health.session_id} instead"
            )
            logger.error("Bridge tunnel reached another session's simulator", extra=extra)
            return self._down(tunnel)
        tunnel.failures, tunnel.retry_at, tunnel.error = 0, 0.0, None
        return self._up(
            tunnel,
            health.runtime_id,
            health.ready,
            None if health.ready else "Simulator is loading",
        )

    def _repush(self, tunnel: _Tunnel, reason: str) -> BridgeReport:
        """The bridge lacks this session's credentials: write them again, with backoff."""
        tunnel.pushed = False
        if self.clock() >= tunnel.retry_at:
            alias = tunnel.target.instance_name + self.alias_suffix
            try:
                self._push(tunnel, alias)
            except TunnelError as exc:
                reason = f"{reason}; {exc}"
            self._failed(tunnel, reason)
        return self._up(tunnel, None, False, reason)

    def _push(self, tunnel: _Tunnel, alias: str) -> None:
        payload = session_payload(tunnel.target.session_id, tunnel.target.token)
        try:
            self.ssh.push(alias, self.remote_session_file, payload)
        finally:
            self.beat()
        tunnel.pushed = True

    def _refresh_for(self, alias: str, *, after_failure: bool) -> None:
        last = self._refreshed_at.get(alias)
        now = self.clock()
        if last is not None and not (after_failure and now - last >= REFRESH_MIN_INTERVAL_S):
            return
        try:
            self.refresh()
        except BrevError as exc:
            raise TunnelError(f"brev refresh failed: {exc}") from None
        finally:
            self.beat()
        self._refreshed_at[alias] = now

    def _free_port(self) -> int:
        used = {t.port for t in self._tunnels.values() if t.process is not None}
        for port in self.ports:
            if port not in used and self.port_free(self.bind_host, port):
                return port
        raise TunnelError(f"No free tunnel port among {len(self.ports)} configured ports")

    def _failed(self, tunnel: _Tunnel, error: str) -> None:
        tunnel.failures += 1
        delay = min(BACKOFF_BASE_S * 2 ** (tunnel.failures - 1), BACKOFF_MAX_S)
        tunnel.retry_at = self.clock() + delay
        tunnel.error = error[:ERROR_MAX]

    def _close(self, tunnel: _Tunnel) -> None:
        if tunnel.process is not None:
            try:
                tunnel.process.close()
            except Exception:
                logger.exception("Could not stop ssh tunnel process")
        tunnel.process, tunnel.port = None, None

    def _up(
        self, tunnel: _Tunnel, runtime_id: str | None, ready: bool, error: str | None
    ) -> BridgeReport:
        return BridgeReport(
            self.advertise_host,
            tunnel.port,
            runtime_id,
            ready,
            error[:ERROR_MAX] if error else None,
        )

    def _down(self, tunnel: _Tunnel) -> BridgeReport:
        return BridgeReport(None, None, None, False, tunnel.error)
