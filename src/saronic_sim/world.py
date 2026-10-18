"""Apply runtime commands to the live stage, boat body, and controls on the simulation thread."""
import copy

import numpy as np

import scene
from commands import OVERVIEW, CommandError, check_look_at, pose_transform


class World:
    def __init__(self, stage, config, camera_paths, controls, sliders=None, initial_transform=None):
        self.stage, self.config, self.controls = stage, config, controls
        self.initial_transform = initial_transform
        self.sliders = sliders or {}
        self.camera_paths = {c["name"]: path for c, path in zip(config["cameras"], camera_paths)}
        self.camera_paths[OVERVIEW] = "/World/Overview"
        # Boat camera coordinates are in the boat frame; the overview is in the world frame.
        self.cameras = {
            c["name"]: {"frame": "boat", "position": list(c["position"]), "target": list(c["target"]), "focal_length": float(c["focal_length"])}
            for c in config["cameras"]
        }
        self.cameras[OVERVIEW] = {"frame": "world", "position": list(scene.OVERVIEW_POSITION), "target": list(scene.OVERVIEW_TARGET), "focal_length": scene.OVERVIEW_FOCAL_LENGTH}
        self.environment = {
            "current": list(config["water"]["current"]),
            "sun_intensity": scene.SUN_INTENSITY,
            "sky_intensity": scene.SKY_INTENSITY,
            "sun_rotation": list(scene.SUN_ROTATION),
        }

    def apply(self, command, body, indices):
        """Apply one validated command. body is None while the timeline is stopped."""
        kind = command["type"]
        if kind == "set_controls":
            for key in ("throttle", "steering"):
                if key in command:
                    self.controls[key] = command[key]
                    if key in self.sliders:
                        # Keep the viewer's sliders in sync; their callbacks set the same value.
                        self.sliders[key].model.set_value(command[key])
        elif kind in ("set_pose", "reset"):
            if body is None:
                raise CommandError("Press Play before moving the boat")
            if kind == "reset":
                body.set_transforms(np.array(self.initial_transform, dtype=np.float32), indices)
                body.set_velocities(np.zeros((1, 6), dtype=np.float32), indices)
            else:
                self.place(body, command, indices)
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
        else:
            raise CommandError(f"Unsupported command {kind}")

    def place(self, body, pose, indices):
        """Teleport the boat to a (partial) pose and stop it."""
        current = body.get_transforms()[0]
        transform = pose_transform(current, pose.get("position"), pose.get("heading"), pose.get("orientation_xyzw"))
        body.set_transforms(np.array([transform], dtype=np.float32), indices)
        body.set_velocities(np.zeros((1, 6), dtype=np.float32), indices)

    def snapshot(self, elapsed, playing, boat=None):
        """JSON-ready state; boat is (transform, velocity) or None while stopped."""
        state = {
            "time": float(elapsed),
            "playing": bool(playing),
            "controls": {"throttle": float(self.controls["throttle"]), "steering": float(self.controls["steering"])},
            "cameras": copy.deepcopy(self.cameras),
            "environment": copy.deepcopy(self.environment),
            "boat": None,
        }
        if boat is not None:
            transform, velocity = ([float(v) for v in values] for values in boat)
            state["boat"] = {
                "position": transform[:3],
                "orientation_xyzw": transform[3:],
                "linear_velocity": velocity[:3],
                "angular_velocity": velocity[3:],
            }
        return state

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
