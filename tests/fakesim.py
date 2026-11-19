"""A lockstep simulator without Isaac Sim, for bridge tests.

The World, Controller, CommandQueue, FrameStore, HTTP server and client are the real
ones; only PhysX bodies, USD edits and render products are replaced. The fake physics
pushes every body each step (kinematic ones too), so a passing test shows that held
poses are re-asserted rather than merely left alone.
"""
import threading
from unittest.mock import Mock

import numpy as np

import worldconfig as wc
from commands import CommandQueue, start_server
from config import load_config
from control import Controller
from frames import FrameStore
from world import World

DRIFT_M = 0.01  # Per physics step, applied to every body by FakeBodies.physics_step.


class FakeBodies:
    """omni.physics.tensors RigidBodyView semantics: full-size buffers plus the rows to write."""

    def __init__(self, entity_ids, order=None):
        order = order or list(entity_ids)
        self.prim_paths = [f"/World/Entities/{entity_id}" for entity_id in order]
        self.count = len(order)
        self.transforms = np.tile(np.array([0, 0, 0, 0, 0, 0, 1], dtype=np.float32), (self.count, 1))
        self.velocities = np.zeros((self.count, 6), dtype=np.float32)
        self.forces = []

    def get_transforms(self):
        return self.transforms.copy()

    def get_velocities(self):
        return self.velocities.copy()

    def set_transforms(self, data, rows):
        assert data.shape == self.transforms.shape, "tensor views take full-size buffers"
        self.transforms[rows] = data[rows]

    def set_velocities(self, data, rows):
        assert data.shape == self.velocities.shape
        self.velocities[rows] = data[rows]

    def apply_forces_and_torques_at_position(self, forces, torques, positions, rows, is_global):
        self.forces.append((forces.copy(), rows.copy()))

    def physics_step(self):
        self.transforms[:, 0] += DRIFT_M
        self.transforms[:, 2] -= DRIFT_M


class FakeOps:
    def __init__(self):
        self.cameras, self.lights, self.viewer = {}, [], []

    def update_camera(self, path, camera):
        self.cameras[path] = camera

    def update_lights(self, environment):
        self.lights.append(dict(environment))

    def update_viewer(self, spec, aim):
        self.viewer.append(dict(spec))


def two_boat_world(motion_mode="external"):
    """Two boats and two cameras: the Stage 2 acceptance world."""
    def camera(camera_id, parent, position, target):
        return {
            "id": camera_id,
            "parent_entity_id": parent,
            "pose": {"position_m": position, "orientation_xyzw": wc.look_at_orientation(position, target)},
            "resolution": [32, 24],
            "intrinsics": {"fx": 24.0, "fy": 24.0, "cx": 16.0, "cy": 12.0},
            "clipping_range_m": [0.05, 250.0],
            "channels": ["rgb", "depth"],
        }
    motion = "external" if motion_mode == "external" else "dynamic"
    return {
        "schema_version": 1,
        "seed": 7,
        "motion_mode": motion_mode,
        "physics_hz": 200,
        "entities": [
            {"id": "alpha", "asset_id": "harbor_boat", "pose": {"position_m": [0, 0, 0.3], "orientation_xyzw": [0, 0, 0, 1]}, "motion_type": motion},
            {"id": "bravo", "asset_id": "harbor_boat", "pose": {"position_m": [10, 5, 0.3], "orientation_xyzw": wc.yaw_quaternion(90)}, "motion_type": motion},
            {"id": "marker", "asset_id": "buoy_red", "pose": {"position_m": [20, 0, 0.3], "orientation_xyzw": [0, 0, 0, 1]}, "motion_type": "external"},
        ],
        "cameras": [
            camera("bow", "alpha", [1.2, 0, 1.0], [12, 0, 0.5]),
            camera("tower", None, [-5, -5, 8], [5, 0, 0]),
        ],
        "environment": {"water_level_m": 0.0, "current_mps": [0, 0, 0], "sun_rotation": [-35, -25, 0], "sun_intensity": 2500, "sky_intensity": 700},
    }


class FakeSim:
    """Mirrors runtime.py's loop around the shared Controller, at the scenario's physics rate."""

    def __init__(self, lockstep=True, serve=True):
        self.scenario = load_config()
        self.catalog = wc.assets(self.scenario)
        self.ops = FakeOps()
        self.controls = {"throttle": 0.0, "steering": 0.0, "reset": False}
        self.world = World(self.scenario, self.catalog, self.ops, self.controls, None, {"position": [-10, -13, 10], "target": [5, 3, 0], "focal_length": 50.0})
        self.commands = CommandQueue()
        self.frames = FrameStore(prefix="t-")
        self.loads = 0
        self.physics_steps = 0
        initial = wc.world_from_scenario(self.scenario)
        self.body = self._build(initial)
        self.world.load(initial, self.body, self.body.prim_paths, self._paths(initial))
        self.controller = Controller(self.world, self.commands, self.frames, self.capture, self.load_world, lockstep)
        self.commands.submit({"type": "probe"})
        self.running = True
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        self.server = start_server(self.commands, 0, self.catalog, self.frames) if serve else None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _build(self, world_config):
        ids = [e["id"] for e in world_config["entities"]]
        # Reverse the order: the world must map rows by prim path, not by list position.
        return FakeBodies(ids, list(reversed(ids)))

    def _paths(self, world_config):
        return {c["id"]: f"/cams/{c['id']}" for c in world_config["cameras"]}

    def capture(self, plan):
        value = self.world.step_index % 256
        captured = {}
        for camera, channels in plan:
            width, height = camera["resolution"]
            captured[camera["id"]] = {}
            if "rgb" in channels:
                captured[camera["id"]]["rgb"] = np.full((height, width, 3), value, dtype=np.uint8)
            if "depth" in channels:
                captured[camera["id"]]["depth"] = np.full((height, width), 7.5, dtype=np.float32)
        return captured

    def load_world(self, world_config):
        self.world.check_runtime(world_config)
        self.world.detach()
        self.body = self._build(world_config)
        self.loads += 1
        result = self.world.load(world_config, self.body, self.body.prim_paths, self._paths(world_config))
        return {**result, "entity_ids": [e["id"] for e in world_config["entities"]], "camera_ids": [c["id"] for c in world_config["cameras"]]}

    def loop(self):
        while self.running:
            with self.lock:
                self.controller.service()
                if self.controller.step_due:
                    self.controller.finish_step()
                elif not self.controller.waiting:
                    self.world.hold()
                    self.world.apply_forces(lambda *args: (np.zeros(3), np.zeros(3)))
                    self.body.physics_step()
                    self.physics_steps += 1
                    self.world.hold()
                    self.world.advance()
                    self.controller.stepped()
                    continue
            threading.Event().wait(0.001)

    def stop(self):
        self.running = False
        self.thread.join(2)
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def fake_world(scenario=None, ops=None, controls=None):
    """A World loaded with the default harbor world and fake bodies, without a thread."""
    scenario = scenario or load_config()
    catalog = wc.assets(scenario)
    controls = controls if controls is not None else {"throttle": 0.0, "steering": 0.0, "reset": False}
    world = World(scenario, catalog, ops or FakeOps(), controls, {"throttle": Mock()}, {"position": [-10, -13, 10], "target": [5, 3, 0], "focal_length": 50.0})
    initial = wc.world_from_scenario(scenario)
    body = FakeBodies([e["id"] for e in initial["entities"]])
    world.load(initial, body, body.prim_paths, {c["id"]: f"/cams/{c['id']}" for c in initial["cameras"]})
    return world, body
