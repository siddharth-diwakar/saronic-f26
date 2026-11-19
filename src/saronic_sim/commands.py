"""The control bridge inside the simulator: request validation, the command queue, and HTTP.

Independent of Kit so it is testable without Isaac Sim. The HTTP threads only validate
and queue; the simulation loop applies commands on its own thread, because USD and PhysX
must not be modified from other threads. A queued response means accepted, not applied:
poll GET /v1/commands/{id} (or pass ?wait=seconds) for the applied result.

Routes (the backend forwards /api/v1/sessions/{id}/... to the matching /v1/... route):
  GET   /health                      runtime_id, ready, episode_id
  PUT   /v1/world                    load a complete world configuration
  PATCH /v1/entities/{id}/pose       place a vessel or obstacle
  PATCH /v1/cameras/{id}             camera mount pose, intrinsics, clipping range
  PATCH /v1/environment              current and lighting
  POST  /v1/step                     apply poses, advance dt_ns exactly, capture cameras
  POST  /v1/render                   capture cameras without advancing time
  POST  /v1/reset                    restore the world baseline into a new episode
  GET   /v1/state                    current episode, time, revision, entity poses
  GET   /v1/commands/{id}            command status and result
  GET   /v1/frames/{id}              one captured image, as binary
  POST  /commands                    the original single-boat commands (Gym client, curl)
"""
import copy
import itertools
import json
import queue
import re
import threading
import uuid
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import worldconfig as wc
from errors import CommandError

MAX_BODY_BYTES = 256 * 1024
RESULT_HISTORY = 1024
MAX_WAIT_SECONDS = 300
MAX_KEY_LENGTH = 128
OVERVIEW = "Overview"
IDEMPOTENCY_HEADER = "Idempotency-Key"
RUNTIME_HEADER = "X-Saronic-Runtime-Id"
DONE = ("applied", "failed")
LEGACY_FIELDS = {
    "set_controls": {"throttle", "steering"},
    "set_pose": {"position", "heading", "orientation_xyzw"},
    "reset": set(),
    "set_camera": {"name", "position", "target", "focal_length"},
    "set_environment": {"current", "sun_intensity", "sky_intensity", "sun_rotation"},
    # Lockstep only: optionally place the boat, advance time, render every camera.
    "step": {"seconds", "pose", "physics", "images", "depth"},
}
POSE_FIELDS = {"position", "heading", "orientation_xyzw"}
CHECK_FIELDS = {"runtime_id", "episode_id", "expected_step", "expected_revision"}
COMMAND_TYPES = {
    "probe", "load_world", "set_entity_pose", "update_camera", "set_viewer_camera",
    "update_environment", "set_controls", "step", "render", "reset",
}

_number = wc.number
_vector = wc.vector


def _legacy_pose(fields):
    pose = {}
    if "position" in fields:
        pose["position"] = _vector(fields["position"], "position")
    if "heading" in fields and "orientation_xyzw" in fields:
        raise CommandError("Give heading or orientation_xyzw, not both")
    if "heading" in fields:
        pose["heading"] = _number(fields["heading"], "heading")
    if "orientation_xyzw" in fields:
        pose["orientation_xyzw"] = wc.quaternion(fields["orientation_xyzw"], "orientation_xyzw")
    return pose


def parse_command(payload):
    """Validate an original /commands body and translate it to the typed internal command.

    Optional fields are omitted from the result, so an update changes only what it names.
    Single-boat commands act on the world's first entity.
    """
    if not isinstance(payload, dict) or payload.get("type") not in LEGACY_FIELDS:
        raise CommandError("type must be one of: " + ", ".join(LEGACY_FIELDS), field="type")
    kind = payload["type"]
    unknown = set(payload) - LEGACY_FIELDS[kind] - {"type"}
    if unknown:
        raise CommandError(f"Unknown fields for {kind}: {', '.join(sorted(unknown))}", code="unknown_field", field=sorted(unknown)[0])
    fields = {key: value for key, value in payload.items() if key != "type"}
    if kind == "reset":
        return {"type": "reset", "checks": {}}
    if kind == "step":
        command = {
            "type": "step",
            "dt_ns": wc.seconds_to_ns(fields.get("seconds", 0.02)),
            "entity_updates": [],
            "checks": {},
        }
        if "pose" in fields:
            if not isinstance(fields["pose"], dict) or set(fields["pose"]) - POSE_FIELDS:
                raise CommandError("pose may contain only: " + ", ".join(sorted(POSE_FIELDS)), field="pose")
            pose = _legacy_pose(fields["pose"])
            if not pose:
                raise CommandError("pose must set position, heading, or orientation_xyzw", field="pose")
            command["entity_updates"] = [{"entity_id": None, "partial": pose}]
        for key, default in (("physics", True), ("images", True), ("depth", False)):
            if not isinstance(fields.get(key, default), bool):
                raise CommandError(f"{key} must be true or false", field=key)
        command["physics"] = fields.get("physics", True)
        command["camera_ids"] = None if fields.get("images", True) else []
        command["channels"] = ["rgb", "depth"] if fields.get("depth", False) else ["rgb"]
        return command
    if not fields or fields.keys() == {"name"}:
        names = LEGACY_FIELDS[kind] - {"name"}
        raise CommandError(f"{kind} requires at least one of: {', '.join(sorted(names))}")
    if kind == "set_controls":
        return {"type": "set_controls", **{key: _number(value, key, -1, 1) for key, value in fields.items()}}
    if kind == "set_pose":
        return {"type": "set_entity_pose", "entity_id": None, "partial": _legacy_pose(fields), "checks": {}}
    if kind == "set_camera":
        name = fields.pop("name", None)
        if not isinstance(name, str):
            raise CommandError("name must be a camera name", field="name")
        look = {key: _vector(fields[key], key) for key in ("position", "target") if key in fields}
        if "focal_length" in fields:
            look["focal_length"] = _number(fields["focal_length"], "focal_length", 0.1, 1000)
        if name == OVERVIEW:
            return {"type": "set_viewer_camera", **look}
        return {"type": "update_camera", "camera_id": name, "legacy": look, "checks": {}}
    changes = {}
    if "current" in fields:
        changes["current_mps"] = _vector(fields["current"], "current")
    if "sun_rotation" in fields:
        changes["sun_rotation"] = _vector(fields["sun_rotation"], "sun_rotation")
    for key in ("sun_intensity", "sky_intensity"):
        if key in fields:
            changes[key] = _number(fields[key], key, 0, 100000)
    return {"type": "update_environment", "changes": changes, "checks": {}}


def _checks(body, allowed):
    checks = {}
    if "episode_id" in allowed and "episode_id" in body:
        if not isinstance(body["episode_id"], str) or not body["episode_id"]:
            raise CommandError("episode_id must be a string", field="episode_id")
        checks["episode_id"] = body["episode_id"]
    for key in ("expected_step", "expected_revision"):
        if key in allowed and key in body:
            checks[key] = wc.integer(body[key], key, 0)
    return checks


def _id_list(value, field, limit):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > limit or len(set(map(str, value))) != len(value):
        raise CommandError(f"{field} must list at most {limit} distinct ids", field=field)
    return [wc.identifier(item, field) for item in value]


def _channels(value, field="channels"):
    if value is None:
        return ["rgb"]
    if not isinstance(value, list) or not value or len(set(value)) != len(value) or set(value) - set(wc.CHANNELS):
        raise CommandError("channels must list one or more of: " + ", ".join(wc.CHANNELS), field=field)
    return [c for c in wc.CHANNELS if c in value]


def parse_request(method, path, body, catalog):
    """Translate a /v1 request into a typed internal command, or None when no route matches."""
    if method == "PUT" and path == "/v1/world":
        return {"type": "load_world", "world": wc.parse_world(body, catalog)}
    match = re.fullmatch(r"/v1/entities/([^/]+)/pose", path)
    if method == "PATCH" and match:
        wc.fields(body, {"pose"} | CHECK_FIELDS - {"expected_step"}, "", ("pose",))
        return {
            "type": "set_entity_pose",
            "entity_id": wc.identifier(match.group(1), "entity_id"),
            "pose": wc.parse_pose(body["pose"], "pose", wc.WORLD_FRAME),
            "checks": _checks(body, CHECK_FIELDS),
        }
    match = re.fullmatch(r"/v1/cameras/([^/]+)", path)
    if method == "PATCH" and match:
        wc.fields(body, wc.LIVE_CAMERA_FIELDS | {"resolution", "channels", "parent_entity_id", "id"} | CHECK_FIELDS - {"expected_step"}, "")
        changes = {key: value for key, value in body.items() if key not in CHECK_FIELDS}
        # Field-level validation needs the camera's resolution and frame: done on the simulation thread.
        return {"type": "update_camera", "camera_id": wc.identifier(match.group(1), "camera_id"), "changes": changes, "checks": _checks(body, CHECK_FIELDS)}
    if method == "PATCH" and path == "/v1/environment":
        changes = {key: value for key, value in body.items() if key not in CHECK_FIELDS} if isinstance(body, dict) else body
        return {"type": "update_environment", "changes": wc.parse_environment(changes, "", partial=True), "checks": _checks(body, CHECK_FIELDS)}
    if method != "POST":
        return None
    if path == "/v1/step":
        wc.fields(body, {"dt_ns", "entity_updates", "camera_ids", "channels"} | CHECK_FIELDS, "", ("dt_ns",))
        updates, seen = [], set()
        raw = body.get("entity_updates", [])
        if not isinstance(raw, list) or len(raw) > wc.MAX_ENTITIES:
            raise CommandError(f"entity_updates must list at most {wc.MAX_ENTITIES} updates", field="entity_updates")
        for index, update in enumerate(raw):
            field = f"entity_updates[{index}]"
            wc.fields(update, {"entity_id", "pose"}, field, ("entity_id", "pose"))
            entity_id = wc.identifier(update["entity_id"], field + ".entity_id")
            if entity_id in seen:
                raise CommandError(f"{entity_id} is updated twice", field=field + ".entity_id")
            seen.add(entity_id)
            updates.append({"entity_id": entity_id, "pose": wc.parse_pose(update["pose"], field + ".pose", wc.WORLD_FRAME)})
        return {
            "type": "step",
            "dt_ns": wc.integer(body["dt_ns"], "dt_ns", 1, wc.MAX_STEP_NS),
            "entity_updates": updates,
            "camera_ids": _id_list(body.get("camera_ids"), "camera_ids", wc.MAX_CAMERAS),
            "channels": _channels(body.get("channels")),
            "physics": True,
            "checks": _checks(body, CHECK_FIELDS),
        }
    if path == "/v1/render":
        wc.fields(body, {"camera_ids", "channels", "runtime_id", "episode_id", "expected_revision"}, "")
        return {
            "type": "render",
            "camera_ids": _id_list(body.get("camera_ids"), "camera_ids", wc.MAX_CAMERAS),
            "channels": _channels(body.get("channels")),
            "checks": _checks(body, {"episode_id", "expected_revision"}),
        }
    if path == "/v1/reset":
        wc.fields(body, {"runtime_id", "expected_revision", "seed"}, "")
        command = {"type": "reset", "checks": _checks(body, {"expected_revision"})}
        if body.get("seed") is not None:
            command["seed"] = wc.integer(body["seed"], "seed", 0, 2**32 - 1)
        return command
    return None


class CommandQueue:
    """Thread-safe hand-off from HTTP threads to the simulation loop, plus command results."""

    def __init__(self, runtime_id=None):
        self.runtime_id = runtime_id or uuid.uuid4().hex
        self._pending = queue.SimpleQueue()
        self._results = OrderedDict()
        self._keys = OrderedDict()  # Idempotency-Key -> (command id, command)
        self._lock = threading.Condition()
        self._ids = itertools.count(1)
        self._state = None
        self._ready = False

    def submit(self, command, key=None):
        """Queue a command and return its ID. A repeated idempotency key returns the first ID."""
        with self._lock:
            if key is not None:
                if key in self._keys:
                    command_id, first = self._keys[key]
                    if first != command:
                        raise CommandError(f"{IDEMPOTENCY_HEADER} was already used for a different request", code="idempotency_conflict")
                    return command_id
            command_id = next(self._ids)
            self._results[command_id] = {"command_id": command_id, "status": "queued"}
            while len(self._results) > RESULT_HISTORY:
                self._results.popitem(last=False)
            if key is not None:
                self._keys[key] = (command_id, copy.deepcopy(command))
                while len(self._keys) > RESULT_HISTORY:
                    self._keys.popitem(last=False)
        self._pending.put((command_id, command))
        return command_id

    def drain(self):
        """Yield the commands queued so far without blocking."""
        while True:
            try:
                yield self._pending.get_nowait()
            except queue.Empty:
                return

    def start(self, command_id):
        with self._lock:
            if command_id in self._results and self._results[command_id]["status"] == "queued":
                self._results[command_id]["status"] = "running"

    def complete(self, command_id, error=None, result=None, sim_time_ns=None):
        """Record an outcome. error is a CommandError, or a message for an internal failure."""
        with self._lock:
            if command_id in self._results:
                entry = {"command_id": command_id, "status": "failed" if error else "applied", "applied_sim_time_ns": sim_time_ns}
                if error:
                    entry["error"] = error.body() if isinstance(error, CommandError) else {"code": "internal_error", "message": str(error)}
                elif result is not None:
                    entry["result"] = result
                self._results[command_id] = entry
            self._lock.notify_all()

    def fail_pending(self, message, code="runtime_stopped"):
        """Fail every queued or running command, for when the simulation can no longer apply them."""
        for command_id, _ in self.drain():
            self.complete(command_id, CommandError(message, code=code))
        with self._lock:
            running = [i for i, r in self._results.items() if r["status"] in ("queued", "running")]
        for command_id in running:
            self.complete(command_id, CommandError(message, code=code))

    def wait(self, command_id, timeout):
        """Block until the command is applied or failed, or the timeout passes."""
        with self._lock:
            self._lock.wait_for(lambda: self._results.get(command_id, {}).get("status") not in ("queued", "running"), timeout)
            return copy.deepcopy(self._results.get(command_id))

    def result(self, command_id):
        with self._lock:
            return copy.deepcopy(self._results.get(command_id))

    def mark_ready(self):
        """Called by the simulation thread once it has serviced a command against the loaded scene."""
        with self._lock:
            self._ready = True

    @property
    def ready(self):
        with self._lock:
            return self._ready

    def publish(self, state):
        with self._lock:
            self._state = copy.deepcopy(state)

    def state(self):
        """Latest published state, or None until the bridge is ready."""
        with self._lock:
            return copy.deepcopy(self._state) if self._ready else None


def rejection(error):
    """HTTP-level refusal body. Command results carry the same fields under "error" as an object."""
    body = {"error": error.code, "message": str(error)}
    if error.field:
        body["field"] = error.field
    return body


def _error(status, code, message, **extra):
    return status, {"error": code, "message": message, **extra}


def make_handler(commands, catalog=None, frames=None):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorize(self):
            """Hook for access.guard: reply and return False to refuse the request."""
            return True

        def _route_command_result(self, path):
            for prefix in ("/v1/commands/", "/commands/"):
                if path.startswith(prefix) and path[len(prefix):].isdigit():
                    result = commands.result(int(path[len(prefix):]))
                    if result:
                        self._reply(HTTPStatus.OK, result)
                    else:
                        self._reply(*_error(HTTPStatus.NOT_FOUND, "unknown_command", "No command with this ID in this runtime", runtime_id=commands.runtime_id))
                    return True
            return False

        def do_GET(self):
            if not self._authorize():
                return
            path = urlsplit(self.path).path
            if path == "/health":
                state = commands.state()
                return self._reply(HTTPStatus.OK, {"runtime_id": commands.runtime_id, "ready": commands.ready, "episode_id": state and state["episode_id"]})
            if path in ("/state", "/v1/state"):
                state = commands.state()
                if state is None:
                    return self._reply(*_error(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", "Simulation not ready"))
                return self._reply(HTTPStatus.OK, state)
            if path.startswith("/v1/frames/") and frames is not None:
                frame = frames.get(path[len("/v1/frames/"):])
                if frame is None:
                    return self._reply(*_error(HTTPStatus.NOT_FOUND, "unknown_frame", "Frame not found; frames are kept for a limited time"))
                data, content_type, headers = frame
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)
                return
            if not self._route_command_result(path):
                self._reply(*_error(HTTPStatus.NOT_FOUND, "not_found", "Not found"))

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                raise CommandError(f"Body must be at most {MAX_BODY_BYTES} bytes")
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise CommandError(f"Body is not valid JSON: {error}") from None

        def _mutate(self, method):
            if not self._authorize():
                return
            url = urlsplit(self.path)
            try:
                wait = float(parse_qs(url.query).get("wait", ["0"])[0])
                if not 0 <= wait <= MAX_WAIT_SECONDS:
                    raise ValueError
            except ValueError:
                return self._reply(*_error(HTTPStatus.BAD_REQUEST, "invalid_request", f"wait must be 0 to {MAX_WAIT_SECONDS} seconds", field="wait"))
            key = self.headers.get(IDEMPOTENCY_HEADER)
            if key is not None and not 0 < len(key) <= MAX_KEY_LENGTH:
                return self._reply(*_error(HTTPStatus.BAD_REQUEST, "invalid_request", f"{IDEMPOTENCY_HEADER} must be 1 to {MAX_KEY_LENGTH} characters"))
            try:
                body = self._body()
                if method == "POST" and url.path == "/commands":
                    if not body:
                        raise CommandError("Body must be a command object")
                    command = parse_command(body)
                else:
                    command = parse_request(method, url.path, body, catalog or {})
                    if command is None:
                        return self._reply(*_error(HTTPStatus.NOT_FOUND, "not_found", "Not found"))
                    expected = body.get("runtime_id") if isinstance(body, dict) else None
                    if expected is not None and expected != commands.runtime_id:
                        return self._reply(*_error(HTTPStatus.CONFLICT, "runtime_changed", "The simulator restarted; earlier command outcomes are unknown", runtime_id=commands.runtime_id))
            except CommandError as error:
                return self._reply(HTTPStatus.BAD_REQUEST, rejection(error))
            if not commands.ready:
                return self._reply(*_error(HTTPStatus.SERVICE_UNAVAILABLE, "not_ready", "The simulator has not finished loading the scene"))
            try:
                command_id = commands.submit(command, key)
            except CommandError as error:
                return self._reply(HTTPStatus.CONFLICT, rejection(error))
            if wait:
                result = commands.wait(command_id, wait)
                if result and result["status"] in DONE:
                    return self._reply(HTTPStatus.OK, result)
            self._reply(HTTPStatus.ACCEPTED, commands.result(command_id) or {"command_id": command_id, "status": "queued"})

        def do_POST(self):
            self._mutate("POST")

        def do_PUT(self):
            self._mutate("PUT")

        def do_PATCH(self):
            self._mutate("PATCH")

        def log_message(self, format, *args):
            pass  # One line per request would drown the simulator's own output.

    return Handler


def start_server(commands, port, catalog=None, frames=None, host="127.0.0.1"):
    """Serve in a daemon thread. Localhost only: reach it through the worker's SSH tunnel."""
    server = ThreadingHTTPServer((host, port), make_handler(commands, catalog, frames))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="harbor-control", daemon=True).start()
    return server
