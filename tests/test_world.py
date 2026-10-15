"""Command application logic with USD calls replaced, runnable without Isaac Sim."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

import scene
from commands import CommandError
from config import load_config
from world import World


class FakeBody:
    def __init__(self):
        self.transforms = np.array([[1, 2, 0.3, 0, 0, 0, 1]], dtype=np.float32)
        self.velocities = np.ones((1, 6), dtype=np.float32)

    def get_transforms(self):
        return self.transforms.copy()

    def get_velocities(self):
        return self.velocities.copy()

    def set_transforms(self, transforms, indices):
        self.transforms = transforms

    def set_velocities(self, velocities, indices):
        self.velocities = velocities


class WorldCommands(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.controls = {"throttle": 0.0, "steering": 0.0, "reset": False}
        self.slider = SimpleNamespace(model=Mock())
        self.world = World(Mock(), self.config, ["/World/Boat/Cameras/Forward", "/World/Boat/Cameras/Mast"], self.controls, {"throttle": self.slider})
        self.world._update_camera = Mock()
        self.world._update_lights = Mock()
        self.body = FakeBody()
        self.indices = np.array([0], dtype=np.uint32)

    def test_controls_update_runtime_and_viewer_slider(self):
        self.world.apply({"type": "set_controls", "throttle": 0.6, "steering": -0.2}, self.body, self.indices)
        self.assertEqual((self.controls["throttle"], self.controls["steering"]), (0.6, -0.2))
        self.slider.model.set_value.assert_called_once_with(0.6)

    def test_set_pose_teleports_and_stops_the_boat(self):
        self.world.apply({"type": "set_pose", "position": [5, -3, 0.3], "heading": 180}, self.body, self.indices)
        np.testing.assert_allclose(self.body.transforms[0], [5, -3, 0.3, 0, 0, 1, 0], atol=1e-6)
        np.testing.assert_array_equal(self.body.velocities, 0)

    def test_movement_needs_a_playing_simulation(self):
        for command in ({"type": "set_pose", "heading": 0}, {"type": "reset"}):
            with self.subTest(command=command), self.assertRaises(CommandError):
                self.world.apply(command, None, self.indices)
        self.world.apply({"type": "reset"}, self.body, self.indices)
        self.assertTrue(self.controls["reset"])

    def test_camera_update_merges_and_only_reaims_when_moved(self):
        self.world.apply({"type": "set_camera", "name": "Forward", "focal_length": 30}, self.body, self.indices)
        path, spec = self.world._update_camera.call_args.args
        self.assertEqual(path, "/World/Boat/Cameras/Forward")
        self.assertEqual(spec["position"], self.config["cameras"][0]["position"])
        self.assertFalse(self.world._update_camera.call_args.kwargs["aim"])
        self.world.apply({"type": "set_camera", "name": "Overview", "target": [0, 0, 0]}, self.body, self.indices)
        self.assertTrue(self.world._update_camera.call_args.kwargs["aim"])
        self.assertEqual(self.world.cameras["Overview"]["position"], scene.OVERVIEW_POSITION)
        self.assertEqual(self.world.cameras["Forward"]["focal_length"], 30)

    def test_invalid_camera_aim_leaves_state_unchanged(self):
        before = copy.deepcopy(self.world.cameras)
        with self.assertRaises(CommandError):
            self.world.apply({"type": "set_camera", "name": "Mast", "target": [-0.6, 0, -5]}, self.body, self.indices)
        self.assertEqual(self.world.cameras, before)
        self.world._update_camera.assert_not_called()

    def test_environment_current_feeds_hydrodynamics(self):
        self.world.apply({"type": "set_environment", "current": [0.5, 0, 0], "sun_intensity": 800}, self.body, self.indices)
        self.assertEqual(self.config["water"]["current"], [0.5, 0, 0])
        environment = self.world._update_lights.call_args.args[0]
        self.assertEqual((environment["sun_intensity"], environment["sky_intensity"]), (800, scene.SKY_INTENSITY))

    def test_snapshot_is_json_ready(self):
        state = self.world.snapshot(np.float32(2.5), True, (self.body.get_transforms()[0], self.body.get_velocities()[0]))
        self.assertEqual(state["boat"]["position"], [1.0, 2.0, 0.30000001192092896])
        self.assertIs(type(state["time"]), float)
        self.assertIsNone(self.world.snapshot(0, False)["boat"])
        state["cameras"]["Forward"]["focal_length"] = 1
        self.assertNotEqual(self.world.cameras["Forward"]["focal_length"], 1)


if __name__ == "__main__":
    unittest.main()
