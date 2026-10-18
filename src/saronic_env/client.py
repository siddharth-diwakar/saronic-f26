"""Drive a harbor started with --lockstep: place the boat, step time, read cameras.

Standard library only. Decoding images to arrays additionally needs NumPy and
Pillow; raw PNG bytes are always available.
"""
import base64
import json
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8212"


class HarborError(RuntimeError):
    pass


@dataclass
class Observation:
    """What the environment looks like after a step."""

    time: float
    state: dict
    frames: dict = field(default_factory=dict)

    @property
    def cameras(self):
        return list(self.frames)

    def png(self, camera):
        return base64.b64decode(self.frames[camera]["rgb_png_base64"])

    def rgb(self, camera):
        """RGB image as a uint8 HxWx3 NumPy array."""
        import io

        import numpy as np
        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(self.png(camera))).convert("RGB"))

    def depth(self, camera):
        """Distance to the image plane in meters as a float32 HxW array (step with depth=True)."""
        import numpy as np
        depth = self.frames[camera].get("depth")
        if depth is None:
            raise HarborError("No depth in this observation; step with depth=True")
        return np.frombuffer(base64.b64decode(depth["base64"]), dtype=depth["dtype"]).reshape(depth["shape"])


class HarborEnv:
    def __init__(self, url=DEFAULT_URL, timeout=120):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                message = json.loads(error.read()).get("error", error.reason)
            except ValueError:
                message = error.reason
            raise HarborError(f"{method} {path} failed with HTTP {error.code}: {message}") from None

    def command(self, command):
        """Send one command and wait until the simulation applies it; return its result."""
        status, body = self._request("POST", f"/commands?wait={self.timeout}", command, timeout=self.timeout + 10)
        if status == 202:
            raise HarborError(f"Command {body['id']} was not applied within {self.timeout}s")
        if body["status"] == "failed":
            raise HarborError(body["error"])
        return body.get("result")

    def state(self):
        return self._request("GET", "/state")[1]

    def step(self, pose=None, seconds=0.02, physics=True, images=True, depth=False):
        """Advance time, optionally placing the boat first.

        pose: {"position": [x, y, z], "orientation_xyzw": [...] or "heading": degrees}.
        physics=False holds that pose (kinematic) instead of simulating water forces,
        for when an external boat model supplies every pose.
        """
        command = {"type": "step", "seconds": seconds, "physics": physics, "images": images, "depth": depth}
        if pose is not None:
            command["pose"] = pose
        result = self.command(command)
        return Observation(time=result["state"]["time"], state=result["state"], frames=result.get("frames", {}))

    def render(self, depth=False):
        """Render the current scene without advancing time."""
        return self.step(seconds=0, depth=depth)

    def reset(self, depth=False):
        """Return the boat to its initial pose, stopped, and render."""
        self.command({"type": "reset"})
        return self.render(depth=depth)

    def set_pose(self, **pose):
        self.command({"type": "set_pose", **pose})

    def set_controls(self, **controls):
        self.command({"type": "set_controls", **controls})

    def set_camera(self, name, **settings):
        self.command({"type": "set_camera", "name": name, **settings})

    def set_environment(self, **settings):
        self.command({"type": "set_environment", **settings})
