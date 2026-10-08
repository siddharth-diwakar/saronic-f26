"""Validated scenario settings; usable without Isaac Sim installed."""
import json
import math
from pathlib import Path

DEFAULT_SCENARIO = Path(__file__).resolve().parents[2] / "scenarios" / "harbor.json"


def load_config(path=DEFAULT_SCENARIO):
    config = json.loads(Path(path).read_text())
    expected = {
        "boat": {"length", "width", "height", "mass", "position", "heading", "max_thrust", "linear_drag", "quadratic_drag", "angular_drag"},
        "water": {"level", "density", "current"},
        "simulation": {"physics_hz", "render_hz", "record_hz", "duration", "seed"},
        "cameras": None,
    }
    if set(config) != set(expected):
        raise ValueError("Scenario requires boat, water, simulation, and cameras sections")
    for section, keys in expected.items():
        if keys is not None and set(config[section]) != keys:
            raise ValueError(f"Unexpected or missing settings in {section}")
    def number(value, name, minimum=None):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if minimum is not None and value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
    def vector(value, name, size):
        if not isinstance(value, list) or len(value) != size:
            raise ValueError(f"{name} must contain {size} numbers")
        for item in value:
            number(item, name)
    boat, water, sim = config["boat"], config["water"], config["simulation"]
    for key in ("length", "width", "height", "mass", "max_thrust"):
        number(boat[key], key, 0.001)
    for key in ("linear_drag", "quadratic_drag", "angular_drag"):
        vector(boat[key], key, 3)
        for item in boat[key]:
            number(item, key, 0)
    vector(boat["position"], "position", 3)
    number(boat["heading"], "heading")
    number(water["level"], "level")
    number(water["density"], "density", 1)
    vector(water["current"], "current", 3)
    if boat["mass"] >= water["density"] * boat["length"] * boat["width"] * boat["height"]:
        raise ValueError("Boat displacement cannot support its mass")
    for key in ("physics_hz", "render_hz", "record_hz"):
        number(sim[key], key, 1)
    if sim["render_hz"] > sim["physics_hz"] or sim["record_hz"] > sim["render_hz"]:
        raise ValueError("Rates must satisfy record_hz <= render_hz <= physics_hz")
    ratio = sim["physics_hz"] / sim["render_hz"]
    if not ratio.is_integer():
        raise ValueError("physics_hz must be an integer multiple of render_hz")
    number(sim["duration"], "duration", 0.001)
    if isinstance(sim["seed"], bool) or not isinstance(sim["seed"], int) or sim["seed"] < 0:
        raise ValueError("seed must be a nonnegative integer")
    names = set()
    if not isinstance(config["cameras"], list) or not config["cameras"]:
        raise ValueError("At least one camera is required")
    for camera in config["cameras"]:
        if set(camera) != {"name", "position", "target", "resolution", "focal_length"}:
            raise ValueError("Unexpected or missing camera settings")
        name = camera["name"]
        if not isinstance(name, str) or not name.isidentifier() or name in names:
            raise ValueError("Camera names must be unique USD identifiers")
        names.add(name)
        vector(camera["position"], name, 3)
        vector(camera["target"], name, 3)
        if camera["position"] == camera["target"]:
            raise ValueError("Camera target must differ from its position")
        vector(camera["resolution"], "resolution", 2)
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 16 or v > 4096 for v in camera["resolution"]):
            raise ValueError("Camera resolution must use integers from 16 to 4096")
        number(camera["focal_length"], "focal_length", 0.1)
    return config
