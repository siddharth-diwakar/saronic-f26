"""Live world state on the simulation thread: entities, cameras, environment, episode and time.

Only the simulation thread calls into this class. Stage edits go through `ops`
(scene.UsdOps in Isaac Sim, a fake in tests); rigid bodies through the physics tensor
view `body`, whose rows are mapped to entity IDs by prim path.

- Every completed command reports the simulation time it was applied at.
- External entities are kinematic: Isaac physics never moves them. Their last supplied
  pose is re-written around every physics step as well, so the read-back pose is the
  supplied pose exactly.
- world_revision increases on every configuration change (world load, pose patch,
  camera, environment, reset), so a client's expected_revision detects a stale view.
- Time is integer nanoseconds and only advances in whole physics periods.
"""
import copy
import uuid

import numpy as np

import worldconfig as wc
from errors import CommandError
from commands import OVERVIEW

LEGACY_TARGET_DISTANCE_M = 10.0


class World:
    def __init__(self, scenario, catalog, ops, controls, sliders=None, viewer=None):
        self.scenario, self.catalog, self.ops, self.controls = scenario, catalog, ops, controls
        self.sliders = sliders or {}
        self.initial_controls = {"throttle": controls["throttle"], "steering": controls["steering"]}
        self.viewer = viewer or {}
        self.baseline = None
        self.config = None  # The current world: baseline plus live camera and environment changes.
        self.body = None
        self.rows = {}  # entity id -> tensor view row
        self.camera_paths = {}
        self.targets = {}  # Legacy look-at targets, per camera
        self.held = {}  # entity id -> [x, y, z, qx, qy, qz, qw] written around every physics step
        self.step_held = set()  # dynamic entities held for the current step only (legacy physics=False)
        self.revision = 0
        self.episode_id = None
        self.step_index = 0
        self.sim_time_ns = 0
        self.period_ns = wc.period_ns(scenario["simulation"]["physics_hz"])

    # Loading and attaching --------------------------------------------------------------

    def check_runtime(self, world):
        """Settings fixed for this simulator process; a different value needs a new runtime."""
        if world["physics_hz"] != self.scenario["simulation"]["physics_hz"]:
            raise CommandError(f"physics_hz is fixed at {self.scenario['simulation']['physics_hz']} for this runtime", code="unsupported_change", field="physics_hz")
        level = self.scenario["water"]["level"]
        if world["environment"]["water_level_m"] != level:
            raise CommandError(f"water_level_m is fixed at {level} for this runtime", code="unsupported_change", field="environment.water_level_m")

    def load(self, world, body, prim_paths, camera_paths):
        """Adopt a freshly built world and start its first episode."""
        self.check_runtime(world)
        self.baseline = copy.deepcopy(world)
        self.camera_paths = dict(camera_paths)
        self.attach(body, prim_paths)
        self.revision += 1
        return self.reset(world["seed"], bump=False)

    def attach(self, body, prim_paths):
        """Map tensor rows to entities; called again whenever the timeline restarts."""
        rows = {path.rstrip("/").rsplit("/", 1)[-1]: row for row, path in enumerate(prim_paths)}
        missing = [e["id"] for e in self.baseline["entities"] if e["id"] not in rows]
        if missing or len(rows) != len(self.baseline["entities"]):
            raise RuntimeError(f"Physics view does not match the world entities (missing {missing})")
        self.body, self.rows = body, rows

    def detach(self):
        self.body = None

    # Episode ----------------------------------------------------------------------------

    def reset(self, seed=None, bump=True):
        """Restore the baseline poses, cameras, environment, controls and clock into a new episode."""
        self._require_body()
        np.random.seed(self.baseline["seed"] if seed is None else seed)
        self.config = copy.deepcopy(self.baseline)
        self.targets = {}
        self.held, self.step_held = {}, set()
        poses = {}
        for entity in self.config["entities"]:
            transform = [*entity["pose"]["position_m"], *entity["pose"]["orientation_xyzw"]]
            poses[entity["id"]] = transform
            if entity["motion_type"] == "external":
                self.held[entity["id"]] = transform
        self._write(poses, zero_velocity=True)
        for camera in self.config["cameras"]:
            self.ops.update_camera(self.camera_paths[camera["id"]], camera)
        self.ops.update_lights(self.config["environment"])
        for key, value in self.initial_controls.items():
            self._set_control(key, value)
        self.episode_id = uuid.uuid4().hex
        self.step_index = 0
        self.sim_time_ns = 0
        if bump:
            self.revision += 1
        return {"episode_id": self.episode_id, "world_revision": self.revision, "seed": self.baseline["seed"] if seed is None else seed}

    def check(self, checks):
        """Refuse a command written against a different episode, step, or revision."""
        if "episode_id" in checks and checks["episode_id"] != self.episode_id:
            raise CommandError(f"episode_id {checks['episode_id']} is not the current episode {self.episode_id}", code="stale_episode", field="episode_id")
        if "expected_step" in checks and checks["expected_step"] != self.step_index:
            raise CommandError(f"expected_step {checks['expected_step']} but the episode is at step {self.step_index}", code="stale_step", field="expected_step")
        if "expected_revision" in checks and checks["expected_revision"] != self.revision:
            raise CommandError(f"expected_revision {checks['expected_revision']} but the world is at revision {self.revision}", code="stale_revision", field="expected_revision")

    # Commands that apply immediately ------------------------------------------------------

    def apply(self, command):
        """Apply one validated non-step command and return its result."""
        kind = command["type"]
        self.check(command.get("checks", {}))
        if kind == "probe":
            self._require_body()
            transforms = self.body.get_transforms()
            if not np.isfinite(transforms).all():
                raise CommandError("Entity transforms are not finite", code="internal_error")
            return {"entities": len(self.rows), "episode_id": self.episode_id}
        if kind == "reset":
            result = self.reset(command.get("seed"))
            return {**result, "state": self.snapshot(True)}
        if kind == "set_controls":
            for key in ("throttle", "steering"):
                if key in command:
                    self._set_control(key, command[key])
            return {"controls": dict(self._controls())}
        if kind == "set_entity_pose":
            entity = self._entity(command.get("entity_id"))
            self._require_body()
            transform = self._resolve_pose(entity, command)
            self._place(entity, transform)
            self.revision += 1
            return {"entity_id": entity["id"], "pose": self._pose(self.body.get_transforms()[self.rows[entity["id"]]]), "world_revision": self.revision}
        if kind == "update_camera":
            camera = self._camera(command["camera_id"])
            if "legacy" in command:
                changes = self._legacy_camera(camera, command["legacy"])
            else:
                changes = wc.parse_camera_update(command["changes"], camera)
            updated = {**camera, **changes}
            self.ops.update_camera(self.camera_paths[camera["id"]], updated)
            camera.update(changes)
            self.revision += 1
            return {"camera": self.effective_camera(camera), "world_revision": self.revision}
        if kind == "update_environment":
            environment = {**self.config["environment"], **command["changes"]}
            self.ops.update_lights(environment)
            self.config["environment"] = environment
            self.revision += 1
            return {"environment": copy.deepcopy(environment), "world_revision": self.revision}
        if kind == "set_viewer_camera":
            spec = {**self.viewer, **{key: command[key] for key in ("position", "target", "focal_length") if key in command}}
            wc.look_at_orientation(spec["position"], spec["target"])
            self.ops.update_viewer(spec, aim="position" in command or "target" in command)
            self.viewer = spec
            return {"viewer": copy.deepcopy(spec)}
        raise CommandError(f"Unsupported command {kind}")

    # Stepping and rendering ---------------------------------------------------------------

    def begin_step(self, command):
        """Validate and apply a step's poses; return the number of physics steps to run."""
        self.check(command.get("checks", {}))
        self._require_body()
        steps = wc.physics_steps(command["dt_ns"], self.period_ns)
        plan = self.capture_plan(command)
        updates = [(self._entity(u.get("entity_id")), u) for u in command["entity_updates"]]
        for entity, update in updates:
            transform = self._resolve_pose(entity, update)
            self._place(entity, transform)
            if entity["motion_type"] == "dynamic" and not command.get("physics", True):
                self.step_held.add(entity["id"])
                self.held[entity["id"]] = transform
        return steps, plan

    def capture_plan(self, command):
        """[(camera, channels)] for a step or render; unknown cameras or unconfigured channels fail."""
        ids = command.get("camera_ids")
        cameras = self.config["cameras"] if ids is None else [self._camera(i) for i in ids]
        channels = command.get("channels", ["rgb"])
        plan = []
        for camera in cameras:
            missing = [c for c in channels if c not in camera["channels"]]
            if missing:
                raise CommandError(f"Camera {camera['id']} was configured without {', '.join(missing)}", code="channel_not_configured", field="channels")
            plan.append((camera, list(channels)))
        return plan

    def end_step(self, steps):
        for entity_id in self.step_held:
            self.held.pop(entity_id, None)
        self.step_held = set()
        if steps:
            self.step_index += 1

    def hold(self):
        """Re-write held poses; called before and after every physics step."""
        if self.held and self.body is not None:
            self._write(self.held, zero_velocity=True)

    def apply_forces(self, forces):
        """Hydrodynamics for dynamic entities that are not held this step."""
        dynamic = [e for e in self.config["entities"] if e["motion_type"] == "dynamic" and e["id"] not in self.held]
        if not dynamic:
            return
        transforms, velocities = self.body.get_transforms(), self.body.get_velocities()
        if not np.isfinite(transforms).all() or not np.isfinite(velocities).all():
            raise RuntimeError("Entity physics became non-finite")
        all_forces = np.zeros((len(self.rows), 3), dtype=np.float32)
        all_torques = np.zeros((len(self.rows), 3), dtype=np.float32)
        rows = []
        for entity in dynamic:
            row = self.rows[entity["id"]]
            force, torque = forces(self.physics_config(entity), transforms[row][:3], transforms[row][3:], velocities[row], self.controls["throttle"], self.controls["steering"])
            all_forces[row], all_torques[row] = force, torque
            rows.append(row)
        self.body.apply_forces_and_torques_at_position(all_forces, all_torques, None, np.array(rows, dtype=np.uint32), True)

    def physics_config(self, entity):
        env = self.config["environment"]
        water = {"level": env["water_level_m"], "density": self.scenario["water"]["density"], "current": env["current_mps"]}
        return {"boat": self.catalog[entity["asset_id"]], "water": water}

    def advance(self):
        self.sim_time_ns += self.period_ns

    # Reporting ----------------------------------------------------------------------------

    def effective_camera(self, camera):
        """The calibration and pose the USD camera actually projects with."""
        optics = wc.usd_optics(camera)
        effective = copy.deepcopy(camera)
        effective["intrinsics"] = wc.intrinsics_from_optics(optics, camera["resolution"])
        effective["optical_frame"] = "+Z view direction, +X image right, +Y image down"
        return effective

    def camera_world_pose(self, camera, poses):
        parent = camera["parent_entity_id"]
        if parent is None:
            return copy.deepcopy(camera["pose"])
        if poses.get(parent) is None:
            return None
        return wc.compose_pose(poses[parent]["pose"], camera["pose"])

    def entity_states(self):
        if self.body is None:
            return {e["id"]: None for e in self.config["entities"]}
        transforms, velocities = self.body.get_transforms(), self.body.get_velocities()
        states = {}
        for entity in self.config["entities"]:
            row = self.rows[entity["id"]]
            velocity = [float(v) for v in velocities[row]]
            states[entity["id"]] = {
                "asset_id": entity["asset_id"],
                "motion_type": entity["motion_type"],
                "pose": self._pose(transforms[row]),
                "linear_velocity_mps": velocity[:3],
                "angular_velocity_radps": velocity[3:],
            }
        return states

    def snapshot(self, playing):
        """JSON-ready state. Entity states are None while the timeline is stopped."""
        return {
            "episode_id": self.episode_id,
            "step_index": self.step_index,
            "sim_time_ns": self.sim_time_ns,
            "world_revision": self.revision,
            "playing": bool(playing),
            "motion_mode": self.config["motion_mode"],
            "physics_hz": self.config["physics_hz"],
            "entities": self.entity_states(),
            "cameras": {c["id"]: self.effective_camera(c) for c in self.config["cameras"]},
            "viewer": {OVERVIEW: copy.deepcopy(self.viewer)},
            "environment": copy.deepcopy(self.config["environment"]),
            "controls": self._controls(),
        }

    def frame_record(self, camera, channel, stored, poses):
        frame_id, encoding, width, height = stored
        return {
            "frame_id": frame_id,
            "camera_id": camera["id"],
            "channel": channel,
            "episode_id": self.episode_id,
            "step_index": self.step_index,
            "sim_time_ns": self.sim_time_ns,
            "world_revision": self.revision,
            "camera": self.effective_camera(camera),
            "camera_world_pose": self.camera_world_pose(camera, poses),
            "encoding": encoding,
            "width": width,
            "height": height,
            "url": f"/v1/frames/{frame_id}",
        }

    def observation(self, actual_dt_ns, captured, store, playing=True):
        """State plus frames; captured is {camera_id: {channel: array}}."""
        state = self.snapshot(playing)
        frames = []
        for camera in self.config["cameras"]:
            for channel, array in captured.get(camera["id"], {}).items():
                frames.append(self.frame_record(camera, channel, store.add(channel, array), state["entities"]))
        return {**state, "actual_dt_ns": actual_dt_ns, "frames": frames}

    # Internals ----------------------------------------------------------------------------

    def _require_body(self):
        if self.body is None:
            raise CommandError("The timeline is stopped; press Play first", code="not_playing")

    def _entity(self, entity_id):
        entities = self.config["entities"]
        if entity_id is None:
            return entities[0]
        for entity in entities:
            if entity["id"] == entity_id:
                return entity
        raise CommandError(f"Unknown entity {entity_id}", code="unknown_entity", field="entity_id")

    def _camera(self, camera_id):
        for camera in self.config["cameras"]:
            if camera["id"] == camera_id:
                return camera
        raise CommandError(f"Unknown camera {camera_id}; cameras: {', '.join(c['id'] for c in self.config['cameras'])}", code="unknown_camera", field="camera_id")

    def _resolve_pose(self, entity, command):
        if "pose" in command:
            pose = command["pose"]
            return [*pose["position_m"], *pose["orientation_xyzw"]]
        partial = command["partial"]
        current = self.body.get_transforms()[self.rows[entity["id"]]]
        return wc.pose_transform(current, partial.get("position"), partial.get("heading"), partial.get("orientation_xyzw"))

    def _place(self, entity, transform):
        """Teleport and stop. External entities keep this pose until the next one."""
        if entity["motion_type"] == "external":
            self.held[entity["id"]] = transform
        self._write({entity["id"]: transform}, zero_velocity=True)

    def _write(self, transforms, zero_velocity):
        rows = np.array([self.rows[i] for i in transforms], dtype=np.uint32)
        # Tensor views take full-size buffers plus the rows to apply.
        data = self.body.get_transforms().copy()
        for entity_id, transform in transforms.items():
            data[self.rows[entity_id]] = transform
        self.body.set_transforms(data.astype(np.float32), rows)
        if zero_velocity:
            # Kinematic bodies reject velocity writes.
            dynamic = np.array([self.rows[e["id"]] for e in self.config["entities"] if e["id"] in transforms and e["motion_type"] == "dynamic"], dtype=np.uint32)
            if len(dynamic):
                velocities = self.body.get_velocities().copy()
                velocities[dynamic] = 0
                self.body.set_velocities(velocities.astype(np.float32), dynamic)

    def _pose(self, transform):
        values = [float(v) for v in transform]
        return {"frame_id": wc.WORLD_FRAME, "position_m": values[:3], "orientation_xyzw": values[3:]}

    def _legacy_camera(self, camera, look):
        changes = {}
        if "position" in look or "target" in look:
            position = look.get("position", camera["pose"]["position_m"])
            target = look.get("target") or self.targets.get(camera["id"])
            if target is None:
                forward = wc.optical_forward(camera["pose"]["orientation_xyzw"])
                target = [p + LEGACY_TARGET_DISTANCE_M * f for p, f in zip(camera["pose"]["position_m"], forward)]
            changes["pose"] = {"frame_id": camera["pose"]["frame_id"], "position_m": list(position), "orientation_xyzw": wc.look_at_orientation(position, target)}
            self.targets[camera["id"]] = list(target)
        if "focal_length" in look:
            changes["intrinsics"] = wc.focal_length_intrinsics(look["focal_length"], camera["resolution"])
        return changes

    def _set_control(self, key, value):
        self.controls[key] = value
        if key in self.sliders:
            # Keep the viewer's sliders in sync; their callbacks set the same value.
            self.sliders[key].model.set_value(value)

    def _controls(self):
        return {"throttle": float(self.controls["throttle"]), "steering": float(self.controls["steering"])}
