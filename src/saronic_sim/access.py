"""Session-token auth and runtime identity for the control bridge (commands.py).

The bridge listens on 127.0.0.1 only. The backend's worker reaches it through an SSH
tunnel, and the worker's forwarded port is reachable from the backend's Docker network,
so requests that arrive through it must prove they belong to this session.

- Runtime ID: random per simulator process. Command IDs restart at 1 when the simulator
  restarts, so callers send the runtime ID they expect (X-Saronic-Runtime-Id) and get 409
  runtime_changed instead of another runtime's command result.
- Session file: the worker writes {"session_id", "token_sha256"} over SSH before it opens
  the tunnel. Requests then need "Authorization: Bearer <token>". Only the hash is on the
  instance. Until a session file first appears, the bridge stays open to localhost (the
  manual workflow over `ssh -L`); once one has been seen it never reopens, and a missing
  or unreadable file then refuses every request.
"""
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from http import HTTPStatus
from urllib.parse import urlsplit

RUNTIME_HEADER = "X-Saronic-Runtime-Id"
HEALTH_PATH = "/health"


def token_sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _parse_session(path):
    """Return (session_id, token_sha256), or None when the file is not a valid session file."""
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session_id, digest = data.get("session_id"), data.get("token_sha256")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    return session_id, digest.lower()


class SessionFile:
    """The worker's session file, re-read whenever it is replaced or changed."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._key = None
        self._value = None
        self._seen = False

    def read(self):
        """Return (session_id, token_sha256), or None when the file is absent or invalid."""
        try:
            stat = os.stat(self.path)
        except OSError:
            key = None
        else:
            # The worker replaces the file with a rename, which changes the inode.
            key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if key != self._key:
                self._key = key
                self._value = None if key is None else _parse_session(self.path)
            self._seen = self._seen or key is not None
            return self._value

    @property
    def seen(self):
        """Whether a session file has existed at any point in this process."""
        with self._lock:
            return self._seen


class Access:
    """Decides whether a bridge request may proceed, and describes this runtime."""

    def __init__(self, session_file, runtime_id=None, clock=time.time):
        self.runtime_id = runtime_id or uuid.uuid4().hex
        self.started_at = clock()
        self.session = SessionFile(session_file)

    def check(self, headers):
        """None when the request may proceed, otherwise (HTTPStatus, error body)."""
        expected = headers.get(RUNTIME_HEADER)
        session = self.session.read()
        if session is None:
            if self.session.seen:
                return HTTPStatus.SERVICE_UNAVAILABLE, {
                    "error": "session_unavailable",
                    "detail": "The session file is missing or unreadable",
                }
        else:
            token = _bearer(headers.get("Authorization"))
            if token is None or not hmac.compare_digest(token_sha256(token), session[1]):
                return HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
        if expected and expected != self.runtime_id:
            return HTTPStatus.CONFLICT, {"error": "runtime_changed", "runtime_id": self.runtime_id}
        return None

    def health(self, commands):
        """Identity and readiness. Ready means the simulation loop has started applying commands."""
        session = self.session.read()
        return {
            "runtime_id": self.runtime_id,
            "session_id": session[0] if session else None,
            "started_at": self.started_at,
            "ready": commands.state() is not None,
        }


def _bearer(value):
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def guard(handler_class, access, commands):
    """Wrap a commands.make_handler class with session auth, runtime headers, and /health."""

    class GuardedHandler(handler_class):
        def end_headers(self):
            self.send_header(RUNTIME_HEADER, access.runtime_id)
            super().end_headers()

        def _allowed(self):
            denied = access.check(self.headers)
            if denied is not None:
                self._reply(*denied)
                return False
            return True

        def do_GET(self):
            if not self._allowed():
                return
            if urlsplit(self.path).path == HEALTH_PATH:
                return self._reply(HTTPStatus.OK, access.health(commands))
            super().do_GET()

        def do_POST(self):
            if self._allowed():
                super().do_POST()

    return GuardedHandler
