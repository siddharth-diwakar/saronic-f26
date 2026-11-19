"""Apply runtime commands to the live stage, boat body, and controls on the simulation thread."""
import copy
import math

import numpy as np

import scene
from commands import OVERVIEW, CommandError, check_look_at, observation_paths, pose_transform


def _heading_orientation(heading):
    half = math.radians(heading) / 2
    return [0.0, 0.0, math.sin(half), math.cos(half)]


class World:
    def __init__(self, stage, config, camera_paths, controls, sliders=None):
        self.stage, self.config, self.controls = stage, config, controls
        self.sliders = sliders or {}
        self.physics_hz = float(config["simulation"]["physics_hz"])
        self.seed = int(config["simulation"]["seed"])
        self.time = 0.0
        self.episode = 1
        boat = config["boat"]
        self.entities = {
            "boat": {
                "kind": "vessel",
                "position": list(boat["position"]),
                "orientation": _heading_orientation(boat["heading"]),
                "linear_velocity": [0.0, 0.0, 0.0],
                "angular_velocity": [0.0, 0.0, 0.0],
            }
        }
        self.camera_paths = {c["name"]: path for c, path in zip(config["cameras"], camera_paths)}
        self.camera_paths[OVERVIEW] = "/World/Overview"
        # Boat camera coordinates are in the boat frame; the overview is in the world frame.
        self.cameras = {
            c["name"]: {
                "frame": "boat",
                "entity_id": "boat",
                "position": list(c["position"]),
                "target": list(c["target"]),
                "focal_length": float(c["focal_length"]),
                "resolution": list(c["resolution"]),
            }
            for c in config["cameras"]
        }
        self.cameras[OVERVIEW] = {
            "frame": "world",
            "entity_id": None,
            "position": list(scene.OVERVIEW_POSITION),
            "target": list(scene.OVERVIEW_TARGET),
            "focal_length": scene.OVERVIEW_FOCAL_LENGTH,
            "resolution": None,
        }
        self.environment = {
            "current": list(config["water"]["current"]),
            "sun_intensity": scene.SUN_INTENSITY,
            "sky_intensity": scene.SKY_INTENSITY,
            "sun_rotation": list(scene.SUN_ROTATION),
        }
        self._initial_controls = {"throttle": float(controls.get("throttle", 0.0)), "steering": float(controls.get("steering", 0.0))}
        self._store_initial()

    def _store_initial(self):
        self._initial = {
            "entities": copy.deepcopy(self.entities),
            "cameras": copy.deepcopy(self.cameras),
            "environment": copy.deepcopy(self.environment),
            "seed": self.seed,
        }

    def apply(self, command, body, indices, command_id=None):
        """Apply one validated command. body is None while the timeline is stopped."""
        kind = command["type"]
        if kind == "set_controls":
            for key in ("throttle", "steering"):
                if key in command:
                    self.controls[key] = command[key]
                    if key in self.sliders:
                        # Keep the viewer's sliders in sync; their callbacks set the same value.
                        self.sliders[key].model.set_value(command[key])
        elif kind in ("set_pose", "reset", "set_entity_pose"):
            if kind == "reset":
                return self._reset(body, indices)
            self._place(command, body, indices)
        elif kind == "set_camera":
            name = command["name"]
            changes = {key: command[key] for key in ("position", "target", "focal_length") if key in command}
            spec = {**self.cameras[name], **changes}
            check_look_at(spec["position"], spec["target"])
            self._update_camera(self.camera_paths[name], spec, aim="position" in changes or "target" in changes)
            self.cameras[name] = spec
        elif kind == "set_environment":
            environment = {**self.environment, **{key: value for key, value in command.items() if key != "type"}}
            self._update_lights(environment)
            # hydrodynamics.forces reads the current from config on every physics step.
            self.config["water"]["current"] = list(environment["current"])
            self.environment = environment
        elif kind == "load_world":
            self._load_world(command, body, indices)
        elif kind == "step":
            return self._step(command, body, indices, command_id)
        elif kind == "render":
            return self._render_observation(command, command_id)
        else:
            raise CommandError(f"Unsupported command {kind}")
        return None

    def snapshot(self, elapsed, playing, boat=None):
        """JSON-ready state; boat is (transform, velocity) or None while stopped."""
        self.time = float(elapsed)
        entities = copy.deepcopy(self.entities)
        state = {
            "time": float(elapsed),
            "episode": self.episode,
            "seed": self.seed,
            "playing": bool(playing),
            "controls": {"throttle": float(self.controls["throttle"]), "steering": float(self.controls["steering"])},
            "cameras": copy.deepcopy(self.cameras),
            "environment": copy.deepcopy(self.environment),
            "entities": entities,
            "boat": None,
        }
        if boat is not None:
            transform, velocity = ([float(v) for v in values] for values in boat)
            vessel = self._primary_vessel()
            pose = {
                "position": transform[:3],
                "orientation": transform[3:],
                "orientation_xyzw": transform[3:],
                "linear_velocity": velocity[:3],
                "angular_velocity": velocity[3:],
            }
            if vessel:
                entities[vessel].update(pose)
                entities[vessel]["kind"] = "vessel"
            state["boat"] = {
                "position": pose["position"],
                "orientation_xyzw": pose["orientation_xyzw"],
                "linear_velocity": pose["linear_velocity"],
                "angular_velocity": pose["angular_velocity"],
            }
        return state

    def _primary_vessel(self):
        for entity_id, entity in self.entities.items():
            if entity["kind"] == "vessel":
                return entity_id
        return None

    def _place(self, command, body, indices):
        if command["type"] == "set_entity_pose":
            entity_id = command["entity_id"]
            if entity_id not in self.entities:
                raise CommandError(f"Unknown entity {entity_id!r}")
            entity = self.entities[entity_id]
            entity["position"] = list(command["position"])
            entity["orientation"] = list(command["orientation"])
            entity["linear_velocity"] = [0.0, 0.0, 0.0]
            entity["angular_velocity"] = [0.0, 0.0, 0.0]
            if entity["kind"] != "vessel":
                return
            if body is None:
                raise CommandError("Press Play before moving the boat")
            current = body.get_transforms()[0]
            transform = pose_transform(current, command["position"], orientation=command["orientation"])
        else:
            if body is None:
                raise CommandError("Press Play before moving the boat")
            current = body.get_transforms()[0]
            transform = pose_transform(current, command.get("position"), command.get("heading"))
            vessel = self._primary_vessel()
            if vessel:
                self.entities[vessel]["position"] = list(transform[:3])
                self.entities[vessel]["orientation"] = list(transform[3:])
                self.entities[vessel]["linear_velocity"] = [0.0, 0.0, 0.0]
                self.entities[vessel]["angular_velocity"] = [0.0, 0.0, 0.0]
        body.set_transforms(np.array([transform], dtype=np.float32), indices)
        body.set_velocities(np.zeros((1, 6), dtype=np.float32), indices)

    def _load_world(self, command, body, indices):
        self.seed = command["seed"]
        self.time = 0.0
        self.episode = 1
        self.entities = {
            entity["id"]: {
                "kind": entity["kind"],
                "position": list(entity["position"]),
                "orientation": list(entity["orientation"]),
                "linear_velocity": [0.0, 0.0, 0.0],
                "angular_velocity": [0.0, 0.0, 0.0],
            }
            for entity in command["entities"]
        }
        cameras = {}
        for camera in command["cameras"]:
            cameras[camera["id"]] = {
                "frame": "boat" if camera.get("entity_id") else "world",
                "entity_id": camera.get("entity_id"),
                "position": list(camera["position"]),
                "target": list(camera["target"]),
                "focal_length": float(camera["focal_length"]),
                "resolution": list(camera["resolution"]),
            }
            path = self.camera_paths.get(camera["id"])
            if path:
                self._update_camera(path, cameras[camera["id"]], aim=True)
        cameras[OVERVIEW] = self.cameras.get(
            OVERVIEW,
            {
                "frame": "world",
                "entity_id": None,
                "position": list(scene.OVERVIEW_POSITION),
                "target": list(scene.OVERVIEW_TARGET),
                "focal_length": scene.OVERVIEW_FOCAL_LENGTH,
                "resolution": None,
            },
        )
        self.cameras = cameras
        environment = dict(command["environment"])
        self._update_lights(environment)
        self.config["water"]["current"] = list(environment["current"])
        self.environment = environment
        self.config["simulation"]["seed"] = self.seed
        self._store_initial()
        vessel = self._primary_vessel()
        if vessel and body is not None:
            self._place(
                {
                    "type": "set_entity_pose",
                    "entity_id": vessel,
                    "position": self.entities[vessel]["position"],
                    "orientation": self.entities[vessel]["orientation"],
                },
                body,
                indices,
            )

    def _reset(self, body, indices):
        if body is None:
            raise CommandError("Press Play before moving the boat")
        self.controls["reset"] = True
        self.controls["throttle"] = self._initial_controls["throttle"]
        self.controls["steering"] = self._initial_controls["steering"]
        for key in ("throttle", "steering"):
            if key in self.sliders:
                self.sliders[key].model.set_value(self.controls[key])
        self.entities = copy.deepcopy(self._initial["entities"])
        self.cameras = copy.deepcopy(self._initial["cameras"])
        self.environment = copy.deepcopy(self._initial["environment"])
        self.config["water"]["current"] = list(self.environment["current"])
        self.seed = self._initial["seed"]
        self.time = 0.0
        self.episode += 1
        self._update_lights(self.environment)
        for name, spec in self.cameras.items():
            path = self.camera_paths.get(name)
            if path:
                self._update_camera(path, spec, aim=True)
        return None

    def _step(self, command, body, indices, command_id):
        steps = int(round(command["dt"] * self.physics_hz))
        if steps < 1:
            raise CommandError("dt must cover at least one physics step")
        for pose in command["poses"]:
            self._place(pose, body, indices)
        self.time += command["dt"]
        return self._render_observation(command, command_id)

    def _render_observation(self, command, command_id):
        cameras = command.get("cameras") or [name for name in self.cameras if name != OVERVIEW]
        observation = {"time": float(self.time), "episode": self.episode, "cameras": {}}
        if command_id is not None:
            observation["cameras"] = observation_paths(command_id, cameras)
        else:
            observation["cameras"] = {name: {} for name in cameras}
        return observation

    def _update_camera(self, path, spec, aim):
        from pxr import UsdGeom
        camera = UsdGeom.Camera(self.stage.GetPrimAtPath(path))
        if aim:
            camera.GetOrderedXformOps()[0].Set(scene.camera_transform(spec["position"], spec["target"]))
        camera.GetFocalLengthAttr().Set(spec["focal_length"])

    def _update_lights(self, environment):
        from pxr import Gf, UsdGeom, UsdLux
        sun = self.stage.GetPrimAtPath("/World/Sun")
        UsdLux.DistantLight(sun).GetIntensityAttr().Set(environment["sun_intensity"])
        UsdGeom.Xformable(sun).GetOrderedXformOps()[0].Set(Gf.Vec3f(*environment["sun_rotation"]))
        UsdLux.DomeLight(self.stage.GetPrimAtPath("/World/Sky")).GetIntensityAttr().Set(environment["sky_intensity"])
