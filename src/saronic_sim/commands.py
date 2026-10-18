"""Runtime commands for a running harbor: validation, queueing, and a local HTTP API.

Independent of Kit so it is testable without Isaac Sim. The HTTP thread only
validates and queues; the simulation loop applies commands on its own thread,
because USD and PhysX must not be modified from other threads.
"""
import copy
import itertools
import json
import math
import queue
import threading
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY_BYTES = 64 * 1024
RESULT_HISTORY = 256
MAX_WAIT_SECONDS = 300
OVERVIEW = "Overview"
COMMAND_FIELDS = {
    "set_controls": {"throttle", "steering"},
    "set_pose": {"position", "heading", "orientation_xyzw"},
    "reset": set(),
    "set_camera": {"name", "position", "target", "focal_length"},
    "set_environment": {"current", "sun_intensity", "sky_intensity", "sun_rotation"},
    # Lockstep only: optionally place the boat, advance time, render every camera.
    "step": {"seconds", "pose", "physics", "images", "depth"},
}
POSE_FIELDS = {"position", "heading", "orientation_xyzw"}


class CommandError(ValueError):
    pass


def _number(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise CommandError(f"{name} must be a finite number")
    if (low is not None and value < low) or (high is not None and value > high):
        raise CommandError(f"{name} must be between {low} and {high}")
    return float(value)


def _vector(value, name, size=3):
    if not isinstance(value, list) or len(value) != size:
        raise CommandError(f"{name} must contain {size} numbers")
    return [_number(item, name) for item in value]


def parse_command(payload, camera_names):
    """Validate a command body and return it normalized; raise CommandError otherwise.

    Optional fields are omitted from the result, so an update changes only what it names.
    """
    if not isinstance(payload, dict) or payload.get("type") not in COMMAND_FIELDS:
        raise CommandError("type must be one of: " + ", ".join(COMMAND_FIELDS))
    kind = payload["type"]
    unknown = set(payload) - COMMAND_FIELDS[kind] - {"type"}
    if unknown:
        raise CommandError(f"Unknown fields for {kind}: {', '.join(sorted(unknown))}")
    fields = {key: value for key, value in payload.items() if key != "type"}
    command = {"type": kind}
    if kind == "set_controls":
        for key, value in fields.items():
            command[key] = _number(value, key, -1, 1)
    elif kind == "set_pose":
        command.update(_pose(fields))
    elif kind == "set_camera":
        if fields.get("name") not in {*camera_names, OVERVIEW}:
            raise CommandError("name must be one of: " + ", ".join([*camera_names, OVERVIEW]))
        command["name"] = fields.pop("name")
        for key in ("position", "target"):
            if key in fields:
                command[key] = _vector(fields[key], key)
        if "focal_length" in fields:
            command["focal_length"] = _number(fields["focal_length"], "focal_length", 0.1, 1000)
    elif kind == "set_environment":
        if "current" in fields:
            command["current"] = _vector(fields["current"], "current")
        if "sun_rotation" in fields:
            command["sun_rotation"] = _vector(fields["sun_rotation"], "sun_rotation")
        for key in ("sun_intensity", "sky_intensity"):
            if key in fields:
                command[key] = _number(fields[key], key, 0, 100000)
    elif kind == "step":
        command["seconds"] = _number(fields.get("seconds", 0.02), "seconds", 0, 60)
        if "pose" in fields:
            if not isinstance(fields["pose"], dict) or set(fields["pose"]) - POSE_FIELDS:
                raise CommandError("pose may contain only: " + ", ".join(sorted(POSE_FIELDS)))
            command["pose"] = _pose(fields["pose"])
            if not command["pose"]:
                raise CommandError("pose must set position, heading, or orientation_xyzw")
        for key, default in (("physics", True), ("images", True), ("depth", False)):
            if not isinstance(fields.get(key, default), bool):
                raise CommandError(f"{key} must be true or false")
            command[key] = fields.get(key, default)
        return command
    if kind != "reset" and len(command) == 1 + (kind == "set_camera"):
        raise CommandError(f"{kind} requires at least one of: {', '.join(sorted(COMMAND_FIELDS[kind] - {'name'}))}")
    return command


def _pose(fields):
    pose = {}
    if "position" in fields:
        pose["position"] = _vector(fields["position"], "position")
    if "heading" in fields and "orientation_xyzw" in fields:
        raise CommandError("Give heading or orientation_xyzw, not both")
    if "heading" in fields:
        pose["heading"] = _number(fields["heading"], "heading")
    if "orientation_xyzw" in fields:
        quaternion = _vector(fields["orientation_xyzw"], "orientation_xyzw", 4)
        norm = math.sqrt(sum(value * value for value in quaternion))
        if norm < 1e-6:
            raise CommandError("orientation_xyzw must be a nonzero quaternion")
        pose["orientation_xyzw"] = [value / norm for value in quaternion]
    return pose


def check_look_at(position, target):
    """Cameras are aimed with +Z up, so they cannot look straight up or down."""
    direction = [t - p for p, t in zip(position, target)]
    if math.hypot(direction[0], direction[1]) < 1e-6 * max(1.0, abs(direction[2])):
        raise CommandError("Camera target must not be directly above or below its position")


def pose_transform(current, position=None, heading=None, orientation_xyzw=None):
    """Return [x, y, z, qx, qy, qz, qw]; a heading alone also levels roll and pitch."""
    transform = [float(value) for value in current]
    if position is not None:
        transform[:3] = position
    if orientation_xyzw is not None:
        transform[3:] = orientation_xyzw
    elif heading is not None:
        half = math.radians(heading) / 2
        transform[3:] = [0.0, 0.0, math.sin(half), math.cos(half)]
    return transform


class CommandQueue:
    """Thread-safe hand-off from HTTP threads to the simulation loop."""

    def __init__(self):
        self._pending = queue.SimpleQueue()
        self._results = OrderedDict()
        self._lock = threading.Condition()
        self._ids = itertools.count(1)
        self._state = None

    def submit(self, command):
        with self._lock:
            command_id = next(self._ids)
            self._results[command_id] = {"id": command_id, "status": "queued"}
            while len(self._results) > RESULT_HISTORY:
                self._results.popitem(last=False)
        self._pending.put((command_id, command))
        return command_id

    def drain(self):
        """Yield the commands queued so far without blocking."""
        while True:
            try:
                yield self._pending.get_nowait()
            except queue.Empty:
                return

    def complete(self, command_id, error=None, result=None):
        with self._lock:
            if command_id in self._results:
                entry = {"id": command_id, "status": "failed" if error else "applied"}
                if error:
                    entry["error"] = error
                elif result is not None:
                    entry["result"] = result
                self._results[command_id] = entry
            self._lock.notify_all()

    def wait(self, command_id, timeout):
        """Block until the command is applied or failed, or the timeout passes."""
        with self._lock:
            self._lock.wait_for(
                lambda: self._results.get(command_id, {}).get("status") != "queued", timeout
            )
            return copy.deepcopy(self._results.get(command_id))

    def result(self, command_id):
        with self._lock:
            return copy.deepcopy(self._results.get(command_id))

    def publish(self, state):
        self._state = copy.deepcopy(state)

    def state(self):
        return self._state


def make_handler(commands, camera_names):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/state":
                state = commands.state()
                if state is None:
                    return self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Simulation not ready"})
                return self._reply(HTTPStatus.OK, state)
            prefix = "/commands/"
            if self.path.startswith(prefix) and self.path[len(prefix):].isdigit():
                result = commands.result(int(self.path[len(prefix):]))
                if result:
                    return self._reply(HTTPStatus.OK, result)
            self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path != "/commands":
                return self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            try:
                wait = float(parse_qs(url.query).get("wait", ["0"])[0])
                if not 0 <= wait <= MAX_WAIT_SECONDS:
                    raise ValueError
            except ValueError:
                return self._reply(HTTPStatus.BAD_REQUEST, {"error": f"wait must be 0 to {MAX_WAIT_SECONDS} seconds"})
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                return self._reply(HTTPStatus.BAD_REQUEST, {"error": f"Body must be 1 to {MAX_BODY_BYTES} bytes"})
            try:
                command = parse_command(json.loads(self.rfile.read(length)), camera_names)
            except (CommandError, json.JSONDecodeError, UnicodeDecodeError) as error:
                return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            command_id = commands.submit(command)
            if wait:
                # ?wait=N blocks until the simulation has applied the command.
                result = commands.wait(command_id, wait)
                if result and result["status"] != "queued":
                    return self._reply(HTTPStatus.OK, result)
            self._reply(HTTPStatus.ACCEPTED, {"id": command_id, "status": "queued"})

        def log_message(self, format, *args):
            pass  # One line per request would drown the simulator's own output.

    return Handler


def start_server(commands, camera_names, port, host="127.0.0.1"):
    """Serve in a daemon thread. Localhost only by default: the API has no authentication."""
    server = ThreadingHTTPServer((host, port), make_handler(commands, camera_names))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="harbor-control", daemon=True).start()
    return server
