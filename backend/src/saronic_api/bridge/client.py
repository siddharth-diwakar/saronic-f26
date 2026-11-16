"""HTTP client for a simulator's control bridge, reached through the worker's SSH tunnel.

The bridge (src/saronic_sim/commands.py with access.py) listens on the instance's
127.0.0.1. Every request carries the session token, and, once known, the runtime ID the
caller expects. Failures that mean "the simulator cannot take this request right now" raise
`BridgeUnavailable`. A restarted simulator raises `BridgeRuntimeChanged`: its command IDs
restart at 1, so results of commands sent to the old runtime are unknown, not lost or applied.
"""

import http.client
import json
from dataclasses import dataclass
from typing import Any

RUNTIME_HEADER = "X-Saronic-Runtime-Id"
HEALTH_PATH = "/health"
_MAX_BODY_BYTES = 16 * 1024 * 1024


class BridgeError(Exception):
    """A bridge request did not produce a bridge response. `code` is stable for API errors."""

    code = "bridge_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class BridgeUnavailable(BridgeError):
    """The tunnel or the simulator is down, starting, or not yet holding this session's token."""

    code = "bridge_unavailable"


class BridgeRuntimeChanged(BridgeError):
    """The simulator restarted since the caller learned its runtime ID."""

    code = "runtime_changed"

    def __init__(self, message: str, *, runtime_id: str | None) -> None:
        super().__init__(message)
        self.runtime_id = runtime_id


@dataclass(frozen=True)
class BridgeHealth:
    runtime_id: str
    session_id: str | None  # None: the bridge has not loaded a session file
    ready: bool  # the simulation loop is applying commands


@dataclass(frozen=True)
class BridgeResponse:
    status: int
    body: Any  # parsed JSON, or None for an empty body
    runtime_id: str | None


class BridgeClient:
    """One bridge. Each call opens its own connection, so a client may be shared across threads."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        runtime_id: str | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._runtime_id = runtime_id
        self._timeout_s = timeout_s

    def health(self) -> BridgeHealth:
        response = self.request("GET", HEALTH_PATH)
        body = response.body
        if response.status != 200 or not isinstance(body, dict) or not body.get("runtime_id"):
            raise BridgeUnavailable(
                f"Bridge health check returned HTTP {response.status}", code="bridge_unhealthy"
            )
        session_id = body.get("session_id")
        return BridgeHealth(
            runtime_id=str(body["runtime_id"]),
            session_id=str(session_id) if session_id else None,
            ready=body.get("ready") is True,
        )

    def request(self, method: str, path: str, body: Any = None) -> BridgeResponse:
        """Send one request. Bridge-level HTTP errors (400, 404, ...) are returned, not raised."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if self._runtime_id:
            headers[RUNTIME_HEADER] = self._runtime_id
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_s)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_BODY_BYTES + 1)
            status, runtime_id = response.status, response.getheader(RUNTIME_HEADER)
        # RemoteDisconnected: the tunnel accepted the connection, but nothing listens on the
        # instance's bridge port (simulator starting or restarting).
        except (OSError, http.client.HTTPException) as exc:
            raise BridgeUnavailable(
                f"Bridge unreachable: {type(exc).__name__}", code="bridge_unreachable"
            ) from None
        finally:
            connection.close()
        if len(raw) > _MAX_BODY_BYTES:
            raise BridgeError("Bridge response is too large", code="bridge_bad_response")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            raise BridgeError("Bridge returned invalid JSON", code="bridge_bad_response") from None
        return self._checked(BridgeResponse(status, parsed, runtime_id))

    def _checked(self, response: BridgeResponse) -> BridgeResponse:
        error = response.body.get("error") if isinstance(response.body, dict) else None
        if response.status == 409 and error == "runtime_changed":
            raise BridgeRuntimeChanged(
                "The simulator restarted; earlier command outcomes are unknown",
                runtime_id=response.runtime_id,
            )
        if response.status == 401:
            raise BridgeUnavailable("Bridge rejected the session token", code="bridge_unauthorized")
        if response.status == 503 and error == "session_unavailable":
            raise BridgeUnavailable(
                "Bridge has lost its session file", code="bridge_session_unavailable"
            )
        return response
