"""World configuration for the bridge API: entities, cameras, environment, and exact time.

Kit-independent, so it is testable without Isaac Sim. Conventions:

- Meters, seconds, and a Z-up world. A vessel's body frame has +X forward, +Y port and
  +Z up, with its origin at the hull center.
- Quaternions are [x, y, z, w] and are normalized on input.
- A camera pose places the camera's optical frame in its parent frame (the parent
  entity's body frame, or the world): +Z along the view direction, +X to the right
  of the image, +Y down the image (the OpenCV convention).
- Intrinsics are in pixels for the configured resolution: fx, fy, cx, cy.
- Simulation time is integer nanoseconds. A step must be a whole number of physics periods,
  so physics_hz must divide one second exactly (100 or 200 Hz for 20 ms steps).
"""
import copy
import math
import re

from errors import CommandError

SCHEMA_VERSION = 1
NS_PER_S = 1_000_000_000
MAX_ENTITIES = 32
MAX_CAMERAS = 8
MAX_STEP_NS = 60 * NS_PER_S
CHANNELS = ("rgb", "depth")
MOTION_MODES = ("external", "internal")
# external: the client supplies every pose and Isaac physics never moves the body.
# dynamic: the approximate hydrodynamics model moves it (internal demo mode only).
MOTION_TYPES = ("external", "dynamic")
HORIZONTAL_APERTURE_MM = 20.955  # USD default; focal lengths are derived from fx with it.
DEFAULT_CLIPPING_M = [0.05, 250.0]
WORLD_FRAME = "world"
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WORLD_FIELDS = {"schema_version", "seed", "motion_mode", "physics_hz", "entities", "cameras", "environment"}
_ENTITY_FIELDS = {"id", "asset_id", "pose", "motion_type"}
_CAMERA_FIELDS = {"id", "parent_entity_id", "pose", "resolution", "intrinsics", "clipping_range_m", "channels"}
ENVIRONMENT_FIELDS = {"water_level_m", "current_mps", "sun_rotation", "sun_intensity", "sky_intensity"}
LIVE_ENVIRONMENT_FIELDS = ENVIRONMENT_FIELDS - {"water_level_m"}
LIVE_CAMERA_FIELDS = {"pose", "intrinsics", "clipping_range_m"}


def number(value, field, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise CommandError(f"{field} must be a finite number", field=field)
    if (low is not None and value < low) or (high is not None and value > high):
        raise CommandError(f"{field} must be between {low} and {high}", field=field)
    return float(value)


def integer(value, field, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandError(f"{field} must be an integer", field=field)
    if (low is not None and value < low) or (high is not None and value > high):
        raise CommandError(f"{field} must be between {low} and {high}", field=field)
    return value


def vector(value, field, size=3):
    if not isinstance(value, list) or len(value) != size:
        raise CommandError(f"{field} must contain {size} numbers", field=field)
    return [number(item, field) for item in value]


def quaternion(value, field):
    q = vector(value, field, 4)
    norm = math.sqrt(sum(v * v for v in q))
    if norm < 1e-6:
        raise CommandError(f"{field} must be a nonzero quaternion", field=field)
    return [v / norm for v in q]


def identifier(value, field):
    if not isinstance(value, str) or not _ID.match(value):
        raise CommandError(f"{field} must start with a letter and use only letters, digits, and _ (max 64)", field=field)
    return value


def fields(payload, allowed, field, required=()):
    if not isinstance(payload, dict):
        raise CommandError(f"{field or 'body'} must be an object", field=field or None)
    unknown = set(payload) - set(allowed)
    if unknown:
        name = sorted(unknown)[0]
        raise CommandError(f"Unknown field {name}", code="unknown_field", field=_join(field, name))
    for name in required:
        if name not in payload:
            raise CommandError(f"{_join(field, name)} is required", field=_join(field, name))
    return payload


def _join(prefix, name):
    return f"{prefix}.{name}" if prefix else name


def parse_pose(payload, field, frame_id):
    """A full pose: position_m and orientation_xyzw are both required."""
    fields(payload, {"frame_id", "position_m", "orientation_xyzw"}, field, ("position_m", "orientation_xyzw"))
    given = payload.get("frame_id", frame_id)
    if given != frame_id:
        raise CommandError(f"{field}.frame_id must be {frame_id!r}", field=_join(field, "frame_id"))
    return {
        "frame_id": frame_id,
        "position_m": vector(payload["position_m"], _join(field, "position_m")),
        "orientation_xyzw": quaternion(payload["orientation_xyzw"], _join(field, "orientation_xyzw")),
    }


def assets(scenario):
    """The approved model catalog. The harbor boat's dimensions come from the scenario."""
    boat = {key: copy.deepcopy(value) for key, value in scenario["boat"].items() if key not in ("position", "heading")}
    return {
        "harbor_boat": {"kind": "boat", **boat},
        "buoy_red": {"kind": "buoy", "radius": 0.3, "height": 1.2, "color": "red"},
        "buoy_green": {"kind": "buoy", "radius": 0.3, "height": 1.2, "color": "green"},
        "marker_box": {"kind": "box", "size": [1.0, 1.0, 1.0], "color": "orange"},
    }


def period_ns(physics_hz):
    if NS_PER_S % physics_hz:
        raise CommandError(
            f"physics_hz must divide one second into whole nanoseconds (for example 100 or 200), not {physics_hz}",
            code="inexact_time", field="physics_hz",
        )
    return NS_PER_S // physics_hz


def physics_steps(dt_ns, period, field="dt_ns"):
    """Whole physics steps in dt_ns. Durations that are not a multiple are rejected, never rounded."""
    if dt_ns % period:
        raise CommandError(
            f"{field} must be a multiple of the physics period {period} ns; {dt_ns} ns is not",
            code="inexact_time", field=field,
        )
    return dt_ns // period


def seconds_to_ns(seconds, field="seconds"):
    """Exact nanoseconds for a decimal duration such as 0.02; finer than 1 ns is rejected."""
    value = number(seconds, field, 0, MAX_STEP_NS / NS_PER_S)
    ns = round(value * NS_PER_S)
    # 1e-3 ns absorbs binary floating point (0.02 * 1e9) without accepting sub-ns durations.
    if abs(ns - value * NS_PER_S) > 1e-3:
        raise CommandError(f"{field} cannot be represented in whole nanoseconds", code="inexact_time", field=field)
    return ns


def parse_environment(payload, field="environment", partial=False):
    allowed = LIVE_ENVIRONMENT_FIELDS if partial else ENVIRONMENT_FIELDS
    if partial and isinstance(payload, dict) and "water_level_m" in payload:
        raise CommandError("water_level_m cannot change while the world is running", code="unsupported_change", field=_join(field, "water_level_m"))
    fields(payload, allowed, field, () if partial else sorted(ENVIRONMENT_FIELDS))
    if partial and not payload:
        raise CommandError("Give at least one of: " + ", ".join(sorted(LIVE_ENVIRONMENT_FIELDS)), field=field)
    environment = {}
    if "water_level_m" in payload:
        environment["water_level_m"] = number(payload["water_level_m"], _join(field, "water_level_m"), -100, 100)
    if "current_mps" in payload:
        environment["current_mps"] = vector(payload["current_mps"], _join(field, "current_mps"))
    if "sun_rotation" in payload:
        environment["sun_rotation"] = vector(payload["sun_rotation"], _join(field, "sun_rotation"))
    for key in ("sun_intensity", "sky_intensity"):
        if key in payload:
            environment[key] = number(payload[key], _join(field, key), 0, 100000)
    return environment


def parse_intrinsics(payload, field, resolution):
    fields(payload, {"fx", "fy", "cx", "cy"}, field, ("fx", "fy", "cx", "cy"))
    width, height = resolution
    return {
        "fx": number(payload["fx"], _join(field, "fx"), 1e-3, 1e6),
        "fy": number(payload["fy"], _join(field, "fy"), 1e-3, 1e6),
        "cx": number(payload["cx"], _join(field, "cx"), 0, width),
        "cy": number(payload["cy"], _join(field, "cy"), 0, height),
    }


def parse_clipping(value, field):
    near, far = vector(value, field, 2)
    if not 0 < near < far:
        raise CommandError(f"{field} must satisfy 0 < near < far", field=field)
    return [near, far]


def parse_camera_update(payload, camera, field=""):
    """Validate a live camera change against the current camera; return the changed fields."""
    if isinstance(payload, dict):
        for name in sorted(set(payload) & (_CAMERA_FIELDS - LIVE_CAMERA_FIELDS)):
            raise CommandError(f"{name} is fixed by the world configuration", code="unsupported_change", field=_join(field, name))
    fields(payload, LIVE_CAMERA_FIELDS, field)
    if not payload:
        raise CommandError("Give at least one of: " + ", ".join(sorted(LIVE_CAMERA_FIELDS)), field=field or None)
    changes = {}
    if "pose" in payload:
        changes["pose"] = parse_pose(payload["pose"], _join(field, "pose"), camera["pose"]["frame_id"])
    if "intrinsics" in payload:
        changes["intrinsics"] = parse_intrinsics(payload["intrinsics"], _join(field, "intrinsics"), camera["resolution"])
    if "clipping_range_m" in payload:
        changes["clipping_range_m"] = parse_clipping(payload["clipping_range_m"], _join(field, "clipping_range_m"))
    return changes


def parse_world(payload, catalog):
    """Validate a complete world configuration and return it normalized."""
    fields(payload, _WORLD_FIELDS, "", sorted(_WORLD_FIELDS))
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CommandError(f"schema_version must be {SCHEMA_VERSION}", field="schema_version")
    motion_mode = payload["motion_mode"]
    if motion_mode not in MOTION_MODES:
        raise CommandError("motion_mode must be one of: " + ", ".join(MOTION_MODES), field="motion_mode")
    physics_hz = integer(payload["physics_hz"], "physics_hz", 10, 1000)
    period_ns(physics_hz)
    world = {
        "schema_version": SCHEMA_VERSION,
        "seed": integer(payload["seed"], "seed", 0, 2**32 - 1),
        "motion_mode": motion_mode,
        "physics_hz": physics_hz,
        "entities": [],
        "cameras": [],
        "environment": parse_environment(payload["environment"]),
    }
    entities = payload["entities"]
    if not isinstance(entities, list) or not 1 <= len(entities) <= MAX_ENTITIES:
        raise CommandError(f"entities must list 1 to {MAX_ENTITIES} entities", field="entities")
    ids = set()
    for index, entity in enumerate(entities):
        field = f"entities[{index}]"
        fields(entity, _ENTITY_FIELDS, field, sorted(_ENTITY_FIELDS))
        entity_id = identifier(entity["id"], field + ".id")
        if entity_id in ids:
            raise CommandError(f"Duplicate entity id {entity_id}", field=field + ".id")
        ids.add(entity_id)
        if entity["asset_id"] not in catalog:
            raise CommandError("asset_id must be one of: " + ", ".join(sorted(catalog)), code="unknown_asset", field=field + ".asset_id")
        motion_type = entity["motion_type"]
        if motion_type not in MOTION_TYPES:
            raise CommandError("motion_type must be one of: " + ", ".join(MOTION_TYPES), field=field + ".motion_type")
        if motion_type == "dynamic":
            if motion_mode != "internal":
                raise CommandError("dynamic entities need motion_mode internal; external mode takes every pose from the client", field=field + ".motion_type")
            if catalog[entity["asset_id"]]["kind"] != "boat":
                raise CommandError("Only boats can be dynamic", field=field + ".motion_type")
        world["entities"].append({
            "id": entity_id,
            "asset_id": entity["asset_id"],
            "pose": parse_pose(entity["pose"], field + ".pose", WORLD_FRAME),
            "motion_type": motion_type,
        })
    cameras = payload["cameras"]
    if not isinstance(cameras, list) or len(cameras) > MAX_CAMERAS:
        raise CommandError(f"cameras must list at most {MAX_CAMERAS} cameras", field="cameras")
    camera_ids = set()
    for index, camera in enumerate(cameras):
        field = f"cameras[{index}]"
        fields(camera, _CAMERA_FIELDS, field, sorted(_CAMERA_FIELDS))
        camera_id = identifier(camera["id"], field + ".id")
        if camera_id in camera_ids:
            raise CommandError(f"Duplicate camera id {camera_id}", field=field + ".id")
        camera_ids.add(camera_id)
        parent = camera["parent_entity_id"]
        if parent is not None and parent not in ids:
            raise CommandError("parent_entity_id must be an entity id or null", code="unknown_entity", field=field + ".parent_entity_id")
        resolution = camera["resolution"]
        if not isinstance(resolution, list) or len(resolution) != 2:
            raise CommandError("resolution must be [width, height]", field=field + ".resolution")
        resolution = [integer(v, field + ".resolution", 16, 4096) for v in resolution]
        channels = camera["channels"]
        if not isinstance(channels, list) or not channels or len(set(channels)) != len(channels) or set(channels) - set(CHANNELS):
            raise CommandError("channels must list one or more of: " + ", ".join(CHANNELS), field=field + ".channels")
        world["cameras"].append({
            "id": camera_id,
            "parent_entity_id": parent,
            "pose": parse_pose(camera["pose"], field + ".pose", parent or WORLD_FRAME),
            "resolution": resolution,
            "intrinsics": parse_intrinsics(camera["intrinsics"], field + ".intrinsics", resolution),
            "clipping_range_m": parse_clipping(camera["clipping_range_m"], field + ".clipping_range_m"),
            "channels": [c for c in CHANNELS if c in channels],
        })
    return world


# Rotation and camera geometry ------------------------------------------------------------

def quat_to_matrix(q):
    """Rotation matrix (rows) for an [x, y, z, w] quaternion; v_parent = M @ v_local."""
    x, y, z, w = q
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def matrix_to_quat(m):
    """[x, y, z, w] with w >= 0 for a proper rotation matrix (rows)."""
    trace = m[0][0] + m[1][1] + m[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1) * 2
        q = [(m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, s / 4]
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = [s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s]
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = [(m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s]
    else:
        s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = [(m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4, (m[1][0] - m[0][1]) / s]
    norm = math.sqrt(sum(v * v for v in q))
    q = [v / norm for v in q]
    return [-v for v in q] if q[3] < 0 else q


def yaw_quaternion(degrees):
    half = math.radians(degrees) / 2
    return [0.0, 0.0, math.sin(half), math.cos(half)]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _normalized(v, what):
    norm = math.sqrt(sum(c * c for c in v))
    if norm < 1e-9:
        raise CommandError(f"{what} has zero length")
    return [c / norm for c in v]


def look_at_orientation(position, target):
    """Optical-frame orientation looking from position at target with the image's up toward +Z."""
    forward = _normalized([t - p for p, t in zip(position, target)], "Camera view direction")
    if math.hypot(forward[0], forward[1]) < 1e-6:
        raise CommandError("Camera target must not be directly above or below its position")
    right = _normalized(_cross(forward, [0.0, 0.0, 1.0]), "Camera right axis")
    down = _cross(forward, right)
    columns = (right, down, forward)
    return matrix_to_quat([[columns[c][r] for c in range(3)] for r in range(3)])


def optical_forward(orientation_xyzw):
    m = quat_to_matrix(orientation_xyzw)
    return [m[0][2], m[1][2], m[2][2]]


def usd_camera_matrix(pose):
    """Gf.Matrix4d rows (row-vector convention) placing a USD camera at an optical-frame pose.

    A USD camera looks down its -Z with +Y up, so its axes are the optical frame's
    (+X, -Y, -Z).
    """
    m = quat_to_matrix(pose["orientation_xyzw"])
    usd_axes = [[m[r][0] for r in range(3)], [-m[r][1] for r in range(3)], [-m[r][2] for r in range(3)]]
    return [[*usd_axes[0], 0.0], [*usd_axes[1], 0.0], [*usd_axes[2], 0.0], [*pose["position_m"], 1.0]]


def usd_optics(camera):
    """USD camera attributes (millimeters) that reproduce pinhole intrinsics in pixels."""
    width, height = camera["resolution"]
    k = camera["intrinsics"]
    focal = k["fx"] * HORIZONTAL_APERTURE_MM / width
    vertical = focal * height / k["fy"]
    return {
        "focal_length": focal,
        "horizontal_aperture": HORIZONTAL_APERTURE_MM,
        "vertical_aperture": vertical,
        "horizontal_aperture_offset": (width / 2 - k["cx"]) * HORIZONTAL_APERTURE_MM / width,
        "vertical_aperture_offset": (k["cy"] - height / 2) * vertical / height,
        "clipping_range": list(camera["clipping_range_m"]),
    }


def intrinsics_from_optics(optics, resolution):
    """Inverse of usd_optics: the calibration a USD camera actually projects with."""
    width, height = resolution
    ha, va, f = optics["horizontal_aperture"], optics["vertical_aperture"], optics["focal_length"]
    return {
        "fx": width * f / ha,
        "fy": height * f / va,
        "cx": width / 2 - optics["horizontal_aperture_offset"] * width / ha,
        "cy": height / 2 + optics["vertical_aperture_offset"] * height / va,
    }


def focal_length_intrinsics(focal_length_mm, resolution):
    """Square-pixel, centered intrinsics for a legacy focal length in millimeters."""
    width, height = resolution
    fx = focal_length_mm * width / HORIZONTAL_APERTURE_MM
    return {"fx": fx, "fy": fx, "cx": width / 2, "cy": height / 2}


def world_from_scenario(scenario):
    """The default world: the scenario's harbor boat and boat cameras, simulated internally."""
    boat = scenario["boat"]
    level = scenario["water"]["level"]
    from scene import SKY_INTENSITY, SUN_INTENSITY, SUN_ROTATION
    return parse_world({
        "schema_version": SCHEMA_VERSION,
        "seed": scenario["simulation"]["seed"],
        "motion_mode": "internal",
        "physics_hz": scenario["simulation"]["physics_hz"],
        "entities": [{
            "id": "boat",
            "asset_id": "harbor_boat",
            "pose": {"position_m": list(boat["position"]), "orientation_xyzw": yaw_quaternion(boat["heading"])},
            "motion_type": "dynamic",
        }],
        "cameras": [{
            "id": c["name"],
            "parent_entity_id": "boat",
            "pose": {"position_m": list(c["position"]), "orientation_xyzw": look_at_orientation(c["position"], c["target"])},
            "resolution": list(c["resolution"]),
            "intrinsics": focal_length_intrinsics(c["focal_length"], c["resolution"]),
            "clipping_range_m": list(DEFAULT_CLIPPING_M),
            "channels": list(CHANNELS),
        } for c in scenario["cameras"]],
        "environment": {
            "water_level_m": level,
            "current_mps": list(scenario["water"]["current"]),
            "sun_rotation": list(SUN_ROTATION),
            "sun_intensity": SUN_INTENSITY,
            "sky_intensity": SKY_INTENSITY,
        },
    }, assets(scenario))


def pose_transform(current, position=None, heading=None, orientation_xyzw=None):
    """Return [x, y, z, qx, qy, qz, qw]; a heading alone also levels roll and pitch."""
    transform = [float(value) for value in current]
    if position is not None:
        transform[:3] = position
    if orientation_xyzw is not None:
        transform[3:] = orientation_xyzw
    elif heading is not None:
        transform[3:] = yaw_quaternion(heading)
    return transform


def rotate(q, v):
    m = quat_to_matrix(q)
    return [sum(m[r][c] * v[c] for c in range(3)) for r in range(3)]


def quat_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ]


def compose_pose(parent, child):
    """World pose of a child pose given in its parent's frame."""
    offset = rotate(parent["orientation_xyzw"], child["position_m"])
    return {
        "frame_id": WORLD_FRAME,
        "position_m": [p + o for p, o in zip(parent["position_m"], offset)],
        "orientation_xyzw": quat_multiply(parent["orientation_xyzw"], child["orientation_xyzw"]),
    }
