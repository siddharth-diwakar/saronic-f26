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
import re
import threading
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

MAX_BODY_BYTES = 64 * 1024
RESULT_HISTORY = 256
OVERVIEW = "Overview"
ENTITY_KINDS = ("vessel", "obstacle")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
ENVIRONMENT_FIELDS = {"current", "sun_intensity", "sky_intensity", "sun_rotation"}
CAMERA_PATCH_FIELDS = {"position", "target", "focal_length"}
COMMAND_FIELDS = {
    "set_controls": {"throttle", "steering"},
    "set_pose": {"position", "heading"},
    "reset": set(),
    "set_camera": {"name", "position", "target", "focal_length"},
    "set_environment": ENVIRONMENT_FIELDS,
}


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


def _orientation(value, name="orientation"):
    quaternion = _vector(value, name, 4)
    norm = math.sqrt(sum(component * component for component in quaternion))
    if norm == 0:
        raise CommandError(f"{name} must be a non-zero quaternion")
    return [component / norm for component in quaternion]


def _stable_id(value, name, taken):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise CommandError(f"{name} must be an identifier")
    if value in taken:
        raise CommandError(f"{name} {value!r} is not unique")
    taken.add(value)
    return value


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
        if "position" in fields:
            command["position"] = _vector(fields["position"], "position")
        if "heading" in fields:
            command["heading"] = _number(fields["heading"], "heading")
    elif kind == "set_camera":
        if fields.get("name") not in {*camera_names, OVERVIEW}:
            raise CommandError("name must be one of: " + ", ".join([*camera_names, OVERVIEW]))
        command["name"] = fields.pop("name")
        _camera_optics(command, fields)
    elif kind == "set_environment":
        _environment_fields(command, fields)
    if kind != "reset" and len(command) == 1 + (kind == "set_camera"):
        raise CommandError(f"{kind} requires at least one of: {', '.join(sorted(COMMAND_FIELDS[kind] - {'name'}))}")
    return command


def _camera_optics(command, fields):
    for key in ("position", "target"):
        if key in fields:
            command[key] = _vector(fields[key], key)
    if "focal_length" in fields:
        command["focal_length"] = _number(fields["focal_length"], "focal_length", 0.1, 1000)


def _environment_fields(command, fields):
    if "current" in fields:
        command["current"] = _vector(fields["current"], "current")
    if "sun_rotation" in fields:
        command["sun_rotation"] = _vector(fields["sun_rotation"], "sun_rotation")
    for key in ("sun_intensity", "sky_intensity"):
        if key in fields:
            command[key] = _number(fields[key], key, 0, 100000)


def parse_world(payload):
    """Validate PUT /world and assign stable entity and camera IDs."""
    if not isinstance(payload, dict):
        raise CommandError("World configuration must be an object")
    unknown = set(payload) - {"seed", "entities", "cameras", "environment"}
    if unknown:
        raise CommandError("Unknown world fields: " + ", ".join(sorted(unknown)))
    if "seed" not in payload:
        raise CommandError("seed is required")
    seed = payload["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise CommandError("seed must be a nonnegative integer")
    entities = _parse_entities(payload.get("entities"))
    cameras = _parse_cameras(payload.get("cameras"), {entity["id"] for entity in entities})
    environment = {"type": "set_environment", **_required_environment(payload.get("environment"))}
    command = {
        "type": "load_world",
        "seed": seed,
        "entities": copy.deepcopy(entities),
        "cameras": copy.deepcopy(cameras),
        "environment": {key: value for key, value in environment.items() if key != "type"},
    }
    catalog = {
        "seed": seed,
        "entities": [{"id": entity["id"], "kind": entity["kind"]} for entity in entities],
        "cameras": [
            {
                "id": camera["id"],
                "entity_id": camera.get("entity_id"),
                "resolution": list(camera["resolution"]),
            }
            for camera in cameras
        ],
    }
    return command, catalog


def _parse_entities(value):
    if not isinstance(value, list) or not value:
        raise CommandError("entities must be a non-empty list")
    taken, entities = set(), []
    counters = {"vessel": 0, "obstacle": 0}
    for item in value:
        if not isinstance(item, dict):
            raise CommandError("each entity must be an object")
        unknown = set(item) - {"id", "kind", "position", "orientation"}
        if unknown:
            raise CommandError("Unknown entity fields: " + ", ".join(sorted(unknown)))
        kind = item.get("kind")
        if kind not in ENTITY_KINDS:
            raise CommandError("entity kind must be vessel or obstacle")
        counters[kind] += 1
        entity_id = item.get("id") or f"{kind}_{counters[kind]}"
        entities.append(
            {
                "id": _stable_id(entity_id, "entity id", taken),
                "kind": kind,
                "position": _vector(item.get("position"), "position") if "position" in item else [0.0, 0.0, 0.0],
                "orientation": _orientation(item["orientation"]) if "orientation" in item else [0.0, 0.0, 0.0, 1.0],
            }
        )
    return entities


def _parse_cameras(value, entity_ids):
    if not isinstance(value, list) or not value:
        raise CommandError("cameras must be a non-empty list")
    taken, cameras = set(), []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise CommandError("each camera must be an object")
        unknown = set(item) - {"id", "entity_id", "position", "target", "focal_length", "resolution"}
        if unknown:
            raise CommandError("Unknown camera fields: " + ", ".join(sorted(unknown)))
        camera_id = _stable_id(item.get("id") or f"camera_{index}", "camera id", taken)
        if camera_id == OVERVIEW:
            raise CommandError("Overview is reserved")
        entity_id = item.get("entity_id")
        if entity_id is not None and entity_id not in entity_ids:
            raise CommandError(f"camera {camera_id} mounts unknown entity {entity_id!r}")
        resolution = item.get("resolution")
        if not isinstance(resolution, list) or len(resolution) != 2:
            raise CommandError("camera resolution must contain two integers")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 16 or v > 4096 for v in resolution):
            raise CommandError("Camera resolution must use integers from 16 to 4096")
        optics = {"id": camera_id, "resolution": list(resolution)}
        if entity_id is not None:
            optics["entity_id"] = entity_id
        if "position" not in item or "target" not in item:
            raise CommandError("camera position and target are required")
        optics["position"] = _vector(item["position"], "position")
        optics["target"] = _vector(item["target"], "target")
        check_look_at(optics["position"], optics["target"])
        optics["focal_length"] = _number(item.get("focal_length", 18), "focal_length", 0.1, 1000)
        cameras.append(optics)
    return cameras


def _required_environment(value):
    if not isinstance(value, dict):
        raise CommandError("environment is required")
    unknown = set(value) - ENVIRONMENT_FIELDS
    if unknown:
        raise CommandError("Unsupported environment fields: " + ", ".join(sorted(unknown)))
    missing = ENVIRONMENT_FIELDS - set(value)
    if missing:
        raise CommandError("environment requires " + ", ".join(sorted(missing)))
    command = {}
    _environment_fields(command, value)
    return command


def parse_entity_pose(entity_id, payload, entity_ids):
    if entity_id not in entity_ids:
        raise CommandError(f"Unknown entity {entity_id!r}")
    if not isinstance(payload, dict):
        raise CommandError("Pose must be an object")
    unknown = set(payload) - {"position", "orientation"}
    if unknown:
        raise CommandError("Unknown pose fields: " + ", ".join(sorted(unknown)))
    if "position" not in payload or "orientation" not in payload:
        raise CommandError("position and orientation are required")
    return {
        "type": "set_entity_pose",
        "entity_id": entity_id,
        "position": _vector(payload["position"], "position"),
        "orientation": _orientation(payload["orientation"]),
    }


def parse_camera_patch(camera_id, payload, camera_names):
    if camera_id not in {*camera_names, OVERVIEW}:
        raise CommandError(f"Unknown camera {camera_id!r}")
    if not isinstance(payload, dict):
        raise CommandError("Camera update must be an object")
    if "resolution" in payload:
        raise CommandError("resolution is fixed at world configuration")
    unknown = set(payload) - CAMERA_PATCH_FIELDS
    if unknown:
        raise CommandError("Unsupported camera fields: " + ", ".join(sorted(unknown)))
    command = {"type": "set_camera", "name": camera_id}
    _camera_optics(command, payload)
    if len(command) == 2:
        raise CommandError("camera update requires position, target, or focal_length")
    return command


def parse_environment_patch(payload):
    if not isinstance(payload, dict):
        raise CommandError("Environment update must be an object")
    unknown = set(payload) - ENVIRONMENT_FIELDS
    if unknown:
        raise CommandError("Unsupported environment fields: " + ", ".join(sorted(unknown)))
    command = {"type": "set_environment"}
    _environment_fields(command, payload)
    if len(command) == 1:
        raise CommandError("environment update requires at least one supported field")
    return command


def parse_step(payload, entity_ids, camera_names):
    if not isinstance(payload, dict):
        raise CommandError("Step must be an object")
    unknown = set(payload) - {"dt", "poses", "cameras"}
    if unknown:
        raise CommandError("Unknown step fields: " + ", ".join(sorted(unknown)))
    dt = _number(payload.get("dt"), "dt", 0.000001)
    poses = payload.get("poses", [])
    if not isinstance(poses, list):
        raise CommandError("poses must be a list")
    parsed = []
    for item in poses:
        if not isinstance(item, dict) or "entity_id" not in item:
            raise CommandError("each external pose needs entity_id, position, and orientation")
        parsed.append(parse_entity_pose(item["entity_id"], item, entity_ids))
    cameras = _selected_cameras(payload.get("cameras"), camera_names)
    return {"type": "step", "dt": dt, "poses": parsed, "cameras": cameras}


def parse_render(payload, camera_names):
    if payload in (None, {}):
        payload = {}
    if not isinstance(payload, dict):
        raise CommandError("Render must be an object")
    unknown = set(payload) - {"cameras"}
    if unknown:
        raise CommandError("Unknown render fields: " + ", ".join(sorted(unknown)))
    return {"type": "render", "cameras": _selected_cameras(payload.get("cameras"), camera_names)}


def parse_reset(payload):
    if payload in (None, {}):
        return {"type": "reset"}
    raise CommandError("reset does not take fields")


def _selected_cameras(value, camera_names):
    names = list(camera_names)
    if value is None:
        return names
    if not isinstance(value, list) or not value:
        raise CommandError("cameras must be a non-empty list")
    selected = []
    for name in value:
        if name not in names:
            raise CommandError(f"Unknown camera {name!r}")
        if name not in selected:
            selected.append(name)
    return selected


def check_look_at(position, target):
    """Cameras are aimed with +Z up, so they cannot look straight up or down."""
    direction = [t - p for p, t in zip(position, target)]
    if math.hypot(direction[0], direction[1]) < 1e-6 * max(1.0, abs(direction[2])):
        raise CommandError("Camera target must not be directly above or below its position")


def pose_transform(current, position=None, heading=None, orientation=None):
    """Return [x, y, z, qx, qy, qz, qw]; a new heading also levels roll and pitch."""
    if heading is not None and orientation is not None:
        raise CommandError("heading and orientation cannot both be set")
    transform = [float(value) for value in current]
    if position is not None:
        transform[:3] = position
    if heading is not None:
        half = math.radians(heading) / 2
        transform[3:] = [0.0, 0.0, math.sin(half), math.cos(half)]
    if orientation is not None:
        transform[3:] = orientation
    return transform


def observation_paths(command_id, camera_ids):
    return {
        camera_id: {
            "rgb": f"/frames/{command_id}/{camera_id}/rgb",
            "depth": f"/frames/{command_id}/{camera_id}/depth",
        }
        for camera_id in camera_ids
    }


class CommandQueue:
    """Thread-safe hand-off from HTTP threads to the simulation loop."""

    def __init__(self, camera_names=None, entity_ids=None):
        self._pending = queue.SimpleQueue()
        self._results = OrderedDict()
        self._frames = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._state = None
        self.camera_ids = list(camera_names or [])
        self.entity_ids = list(entity_ids or ["boat"])
        self.resolutions = {}

    def submit(self, command):
        with self._lock:
            command_id = next(self._ids)
            self._results[command_id] = {"id": command_id, "status": "queued"}
            while len(self._results) > RESULT_HISTORY:
                dropped, _ = self._results.popitem(last=False)
                for key in list(self._frames):
                    if key[0] == dropped:
                        self._frames.pop(key, None)
        self._pending.put((command_id, command))
        return command_id

    def drain(self):
        """Yield the commands queued so far without blocking."""
        while True:
            try:
                yield self._pending.get_nowait()
            except queue.Empty:
                return

    def complete(self, command_id, error=None, observation=None):
        with self._lock:
            if command_id in self._results:
                result = {"id": command_id, "status": "failed" if error else "applied"}
                if error:
                    result["error"] = error
                if observation is not None:
                    result["observation"] = copy.deepcopy(observation)
                self._results[command_id] = result

    def result(self, command_id):
        with self._lock:
            return copy.deepcopy(self._results.get(command_id))

    def store_frames(self, command_id, frames):
        """frames: {camera_id: {"rgb": bytes, "depth": bytes}} -> observation camera map."""
        with self._lock:
            for camera_id, blobs in frames.items():
                if "rgb" in blobs:
                    self._frames[(command_id, camera_id, "rgb")] = ("image/png", blobs["rgb"])
                if "depth" in blobs:
                    self._frames[(command_id, camera_id, "depth")] = (
                        "application/octet-stream",
                        blobs["depth"],
                    )
        return observation_paths(command_id, list(frames))

    def frame(self, command_id, camera_id, kind):
        with self._lock:
            return self._frames.get((command_id, camera_id, kind))

    def publish(self, state):
        self._state = copy.deepcopy(state)

    def state(self):
        return self._state


def make_handler(commands, camera_names):
    if not commands.camera_ids:
        commands.camera_ids = list(camera_names)

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, status, content_type, data):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self, allow_empty=False):
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if allow_empty and length == 0:
                return None
            if not 0 < length <= MAX_BODY_BYTES:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": f"Body must be 1 to {MAX_BODY_BYTES} bytes"})
                return False
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return False

        def _queue(self, command, extra=None):
            command_id = commands.submit(command)
            body = {"id": command_id, "status": "queued"}
            if extra:
                body.update(extra)
            self._reply(HTTPStatus.ACCEPTED, body)

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
                return self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            frame = _parse_frame_path(self.path)
            if frame:
                blob = commands.frame(*frame)
                if blob:
                    return self._bytes(HTTPStatus.OK, blob[0], blob[1])
                return self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            if self.path == "/commands":
                payload = self._read_json()
                if payload is False:
                    return
                try:
                    command = parse_command(payload, commands.camera_ids)
                except CommandError as error:
                    return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return self._queue(command)
            if self.path == "/step":
                return self._validated(lambda body: parse_step(body, commands.entity_ids, commands.camera_ids))
            if self.path == "/render":
                payload = self._read_json(allow_empty=True)
                if payload is False:
                    return
                try:
                    command = parse_render(payload, commands.camera_ids)
                except CommandError as error:
                    return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return self._queue(command)
            if self.path == "/reset":
                payload = self._read_json(allow_empty=True)
                if payload is False:
                    return
                try:
                    command = parse_reset(payload)
                except CommandError as error:
                    return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return self._queue(command)
            self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_PUT(self):
            if self.path != "/world":
                return self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            payload = self._read_json()
            if payload is False:
                return
            try:
                command, catalog = parse_world(payload)
            except CommandError as error:
                return self._reply(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            commands.entity_ids = [entity["id"] for entity in catalog["entities"]]
            commands.camera_ids = [camera["id"] for camera in catalog["cameras"]]
            commands.resolutions = {camera["id"]: camera["resolution"] for camera in catalog["cameras"]}
            self._queue(command, catalog)

        def do_PATCH(self):
            entity_pose = _parse_entity_pose_path(self.path)
            if entity_pose:
                payload = self._read_json()
                if payload is False:
                    return
                try:
                    command = parse_entity_pose(entity_pose, payload, commands.entity_ids)
                except CommandError as error:
                    status = HTTPStatus.NOT_FOUND if str(error).startswith("Unknown entity") else HTTPStatus.BAD_REQUEST
                    return self._reply(status, {"error": str(error)})
                return self._queue(command)
            if self.path.startswith("/cameras/"):
                camera_id = unquote(self.path[len("/cameras/"):])
                if "/" in camera_id or not camera_id:
                    return self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                payload = self._read_json()
                if payload is False:
                    return
                try:
                    command = parse_camera_patch(camera_id, payload, commands.camera_ids)
                except CommandError as error:
                    status = HTTPStatus.NOT_FOUND if str(error).startswith("Unknown camera") else HTTPStatus.BAD_REQUEST
                    return self._reply(status, {"error": str(error)})
                return self._queue(command)
            if self.path == "/environment":
                return self._validated(parse_environment_patch)
            self._reply(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def _validated(self, parser):
            payload = self._read_json()
            if payload is False:
                return
            try:
                command = parser(payload)
            except CommandError as error:
                status = HTTPStatus.NOT_FOUND if str(error).startswith("Unknown ") else HTTPStatus.BAD_REQUEST
                return self._reply(status, {"error": str(error)})
            self._queue(command)

        def log_message(self, format, *args):
            pass  # One line per request would drown the simulator's own output.

    return Handler


def _parse_entity_pose_path(path):
    prefix, suffix = "/entities/", "/pose"
    if path.startswith(prefix) and path.endswith(suffix):
        entity_id = unquote(path[len(prefix) : -len(suffix)])
        if entity_id and "/" not in entity_id:
            return entity_id
    return None


def _parse_frame_path(path):
    parts = path.split("/")
    if len(parts) == 5 and parts[1] == "frames" and parts[2].isdigit() and parts[4] in {"rgb", "depth"}:
        return int(parts[2]), unquote(parts[3]), parts[4]
    return None


def start_server(commands, camera_names, port, host="127.0.0.1"):
    """Serve in a daemon thread. Localhost only by default: the API has no authentication."""
    server = ThreadingHTTPServer((host, port), make_handler(commands, camera_names))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="harbor-control", daemon=True).start()
    return server
