"""Connection from the backend to a session's simulator control bridge.

The worker keeps an SSH tunnel per running session (tunnel.py) and records it on the
session row. The API reads that registration with `bridge_connection()` and talks to the
bridge with `BridgeClient`.
"""

from saronic_api.bridge.client import (
    RUNTIME_HEADER,
    BridgeClient,
    BridgeError,
    BridgeHealth,
    BridgeResponse,
    BridgeRuntimeChanged,
    BridgeUnavailable,
)
from saronic_api.bridge.connection import BridgeConnection, bridge_connection

__all__ = [
    "RUNTIME_HEADER",
    "BridgeClient",
    "BridgeConnection",
    "BridgeError",
    "BridgeHealth",
    "BridgeResponse",
    "BridgeRuntimeChanged",
    "BridgeUnavailable",
    "bridge_connection",
]
