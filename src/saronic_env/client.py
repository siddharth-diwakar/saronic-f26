"""Drive a harbor's control bridge: place entities, step time exactly, read cameras.

Standard library only. Decoding images to arrays additionally needs NumPy and
Pillow; raw PNG bytes are always available.

Every mutating request carries an Idempotency-Key and is retried with the same key
when the response is lost, so a retry never applies a command twice.
"""
import json
import socket
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8212"
RETRIES = 3


class HarborError(RuntimeError):
    def __init__(self, message, code=None, field=None):
        super().__init__(message)
        self.code = code
        self.field = field


class Observation:
    """A bridge observation: state at a step boundary plus references to its frames."""

    def __init__(self, data, env):
        self.data = data
        self._env = env
        self._cache = {}

    @property
    def state(self):
        return self.data

    @property
    def time(self):
        return self.data["sim_time_ns"] / 1e9

    @property
    def cameras(self):
        return list(dict.fromkeys(f["camera_id"] for f in self.data["frames"]))

    def entity(self, entity_id=None):
        """An entity's state; the first entity when entity_id is None."""
        entities = self.data["entities"]
        return entities[entity_id if entity_id is not None else next(iter(entities))]

    def frame(self, camera, channel="rgb"):
        for frame in self.data["frames"]:
            if frame["camera_id"] == camera and frame["channel"] == channel:
                return frame
        raise HarborError(f"No {channel} frame from {camera} in this observation")

    def download(self, camera, channel="rgb"):
        key = (camera, channel)
        if key not in self._cache:
            self._cache[key] = self._env.download(self.frame(camera, channel)["url"])
        return self._cache[key]

    def png(self, camera):
        return self.download(camera, "rgb")

    def rgb(self, camera):
        """RGB image as a uint8 HxWx3 NumPy array."""
        import io

        import numpy as np
        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(self.png(camera))).convert("RGB"))

    def depth(self, camera):
        """Distance to the image plane in meters as a float32 HxW array (step with depth=True)."""
        import numpy as np
        frame = self.frame(camera, "depth")
        return np.frombuffer(self.download(camera, "depth"), dtype="<f4").reshape(frame["height"], frame["width"])


class HarborEnv:
    def __init__(self, url=DEFAULT_URL, timeout=120, headers=None):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})

    def _open(self, method, path, body=None, timeout=None, key=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **self.headers}
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            raw = error.read()
            error.close()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            raise HarborError(
                f"{method} {path} failed with HTTP {error.code}: {parsed.get('message') or parsed.get('error') or error.reason}",
                code=parsed.get("error"), field=parsed.get("field"),
            ) from None

    def _request(self, method, path, body=None, timeout=None):
        """JSON request; mutating requests retry lost responses with one idempotency key."""
        key = uuid.uuid4().hex if method != "GET" else None
        for attempt in range(RETRIES):
            try:
                status, raw, _ = self._open(method, path, body, timeout, key)
                return status, json.loads(raw)
            except (URLError, ConnectionError, socket.timeout, TimeoutError):
                if attempt == RETRIES - 1:
                    raise

    def download(self, url_path):
        return self._open("GET", url_path)[1]

    def _command(self, method, path, body):
        """Send one command and wait until the simulation applies it; return its result."""
        separator = "&" if "?" in path else "?"
        status, result = self._request(method, f"{path}{separator}wait={self.timeout}", body, timeout=self.timeout + 10)
        if result["status"] not in ("applied", "failed"):
            raise HarborError(f"Command {result['command_id']} was not applied within {self.timeout}s", code="timeout")
        if result["status"] == "failed":
            error = result["error"]
            raise HarborError(error["message"], code=error["code"], field=error.get("field"))
        return result.get("result")

    def command(self, command):
        """An original single-boat command such as {"type": "set_controls", "throttle": 0.4}."""
        return self._command("POST", "/commands", command)

    def health(self):
        return self._request("GET", "/health")[1]

    def state(self):
        return self._request("GET", "/v1/state")[1]

    # Typed API ------------------------------------------------------------------------

    def load_world(self, world):
        """PUT /v1/world: returns episode_id, world_revision, entity_ids, camera_ids."""
        return self._command("PUT", "/v1/world", world)

    def place(self, entity_id, position_m, orientation_xyzw, **checks):
        body = {"pose": {"position_m": list(position_m), "orientation_xyzw": list(orientation_xyzw)}, **checks}
        return self._command("PATCH", f"/v1/entities/{entity_id}/pose", body)

    def update_camera(self, camera_id, **changes):
        return self._command("PATCH", f"/v1/cameras/{camera_id}", changes)

    def update_environment(self, **changes):
        return self._command("PATCH", "/v1/environment", changes)

    def advance(self, dt_ns, entity_updates=(), camera_ids=None, channels=("rgb",), **checks):
        """POST /v1/step: apply poses, advance exactly dt_ns, capture the cameras at the boundary."""
        body = {"dt_ns": dt_ns, "entity_updates": list(entity_updates), "channels": list(channels), **checks}
        if camera_ids is not None:
            body["camera_ids"] = list(camera_ids)
        return Observation(self._command("POST", "/v1/step", body), self)

    def render_cameras(self, camera_ids=None, channels=("rgb",)):
        body = {"channels": list(channels)}
        if camera_ids is not None:
            body["camera_ids"] = list(camera_ids)
        return Observation(self._command("POST", "/v1/render", body), self)

    def reset_episode(self, seed=None, **checks):
        body = dict(checks)
        if seed is not None:
            body["seed"] = seed
        return self._command("POST", "/v1/reset", body)

    # Single-boat helpers (the original Gym-style interface) ----------------------------

    def step(self, pose=None, seconds=0.02, physics=True, images=True, depth=False):
        """Advance time, optionally placing the boat first.

        pose: {"position": [x, y, z], "orientation_xyzw": [...] or "heading": degrees}.
        physics=False holds that pose (kinematic) instead of simulating water forces,
        for when an external boat model supplies every pose.
        """
        command = {"type": "step", "seconds": seconds, "physics": physics, "images": images, "depth": depth}
        if pose is not None:
            command["pose"] = pose
        return Observation(self.command(command), self)

    def render(self, depth=False):
        """Render the current scene without advancing time."""
        return self.step(seconds=0, depth=depth)

    def reset(self, depth=False):
        """Start a new episode from the world baseline, and render."""
        self.command({"type": "reset"})
        return self.render(depth=depth)

    def set_pose(self, **pose):
        return self.command({"type": "set_pose", **pose})

    def set_controls(self, **controls):
        return self.command({"type": "set_controls", **controls})

    def set_camera(self, name, **settings):
        return self.command({"type": "set_camera", "name": name, **settings})

    def set_environment(self, **settings):
        return self.command({"type": "set_environment", **settings})
