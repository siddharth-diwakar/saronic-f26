"""How the API reaches a session's simulator: the worker's registration on the session row.

The worker holds the SSH tunnel (and the Brev key); the API connects to the tunnel's port on
the worker and authenticates to the bridge with the session token. Use
`bridge_connection(record).client()` and handle `BridgeError`.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from saronic_api.bridge.client import BridgeClient, BridgeUnavailable
from saronic_api.sessions.status import SessionStatus


class BridgeRecord(Protocol):
    id: uuid.UUID
    status: str
    bridge_token: str
    bridge_host: str | None
    bridge_port: int | None
    bridge_runtime_id: str | None
    bridge_ready: bool
    bridge_error: str | None


@dataclass(frozen=True)
class BridgeConnection:
    session_id: uuid.UUID
    host: str
    port: int
    token: str
    runtime_id: str  # the runtime the worker last verified; commands must target it

    def client(self, timeout_s: float = 5.0) -> BridgeClient:
        """A client that rejects a restarted simulator with `BridgeRuntimeChanged`."""
        return BridgeClient(
            self.host, self.port, self.token, runtime_id=self.runtime_id, timeout_s=timeout_s
        )


def bridge_connection(record: BridgeRecord) -> BridgeConnection:
    """The session's verified bridge, or `BridgeUnavailable` saying why there is none."""
    if record.status != SessionStatus.RUNNING:
        raise BridgeUnavailable(
            f"Session is {record.status}, not running", code="session_not_running"
        )
    if record.bridge_host is None or record.bridge_port is None:
        detail = f": {record.bridge_error}" if record.bridge_error else ""
        raise BridgeUnavailable(
            f"No tunnel to the simulator yet{detail}", code="bridge_not_connected"
        )
    if not record.bridge_ready or not record.bridge_runtime_id:
        detail = f": {record.bridge_error}" if record.bridge_error else ""
        raise BridgeUnavailable(f"Simulator is not ready{detail}", code="bridge_not_ready")
    return BridgeConnection(
        session_id=record.id,
        host=record.bridge_host,
        port=record.bridge_port,
        token=record.bridge_token,
        runtime_id=record.bridge_runtime_id,
    )
