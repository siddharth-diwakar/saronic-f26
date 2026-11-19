"""World command application with USD and PhysX replaced, runnable without Isaac Sim."""
import copy
import json
import unittest

import numpy as np

import worldconfig as wc
from config import load_config
from errors import CommandError
from fakesim import FakeBodies, FakeOps, fake_world, two_boat_world
from world import World

POSE = {"frame_id": "world", "position_m": [5.0, -3.0, 0.3], "orientation_xyzw": wc.yaw_quaternion(180)}


def load_two_boats(motion_mode="external"):
    scenario = load_config()
    catalog = wc.assets(scenario)
    ops = FakeOps()
    world = World(scenario, catalog, ops, {"throttle": 0.0, "steering": 0.0, "reset": False})
    config = wc.parse_world(two_boat_world(motion_mode), catalog)
    ids = [e["id"] for e in config["entities"]]
    body = FakeBodies(ids, list(reversed(ids)))
    world.load(config, body, body.prim_paths, {c["id"]: f"/cams/{c['id']}" for c in config["cameras"]})
    return world, body, ops


class DefaultHarborWorld(unittest.TestCase):
    def setUp(self):
        self.controls = {"throttle": 0.0, "steering": 0.0, "reset": False}
        self.world, self.body = fake_world(controls=self.controls)

    def test_controls_update_runtime_and_viewer_slider(self):
        self.world.apply({"type": "set_controls", "throttle": 0.6, "steering": -0.2})
        self.assertEqual((self.controls["throttle"], self.controls["steering"]), (0.6, -0.2))
        self.world.sliders["throttle"].model.set_value.assert_called_with(0.6)

    def test_set_pose_returns_the_applied_pose_and_stops_the_boat(self):
        self.body.velocities[:] = 1
        revision = self.world.revision
        result = self.world.apply({"type": "set_entity_pose", "entity_id": None, "partial": {"position": [5, -3, 0.3], "heading": 180}})
        np.testing.assert_allclose(self.body.transforms[0], [5, -3, 0.3, 0, 0, 1, 0], atol=1e-6)
        np.testing.assert_array_equal(self.body.velocities, 0)
        self.assertEqual(result["entity_id"], "boat")
        np.testing.assert_allclose(result["pose"]["position_m"], [5, -3, 0.3], atol=1e-6)
        self.assertEqual(result["world_revision"], revision + 1)

    def test_movement_needs_a_playing_simulation(self):
        self.world.detach()
        for command in ({"type": "set_entity_pose", "entity_id": None, "partial": {"heading": 0}}, {"type": "reset", "checks": {}}, {"type": "probe"}):
            with self.subTest(command=command), self.assertRaises(CommandError) as raised:
                self.world.apply(command)
            self.assertEqual(raised.exception.code, "not_playing")

    def test_dynamic_boat_is_driven_by_hydrodynamics_unless_held(self):
        calls = []
        self.world.apply_forces(lambda config, *args: calls.append(config) or (np.ones(3), np.zeros(3)))
        self.assertEqual(calls[0]["boat"]["mass"], 650.0)
        forces, rows = self.body.forces[-1]
        np.testing.assert_array_equal(rows, [0])
        np.testing.assert_array_equal(forces[0], [1, 1, 1])
        # Legacy physics=False: the step holds the supplied pose instead.
        steps, _ = self.world.begin_step({"type": "step", "dt_ns": 20_000_000, "physics": False, "entity_updates": [{"entity_id": None, "partial": {"position": [3, 0, 0.3]}}], "camera_ids": [], "checks": {}})
        self.assertEqual(steps, 4)
        self.world.apply_forces(lambda *args: self.fail("held boats get no forces"))
        self.body.physics_step()
        self.world.hold()
        np.testing.assert_allclose(self.body.transforms[0][:3], [3, 0, 0.3], atol=1e-6)
        self.world.end_step(steps)
        self.assertEqual(self.world.held, {})

    def test_legacy_camera_update_keeps_position_and_aims_on_request(self):
        result = self.world.apply({"type": "update_camera", "camera_id": "Forward", "legacy": {"focal_length": 30}, "checks": {}})
        camera = self.world.ops.cameras["/cams/Forward"]
        self.assertEqual(camera["pose"]["position_m"], [1.2, 0.0, 1.0])
        self.assertAlmostEqual(wc.usd_optics(camera)["focal_length"], 30)
        self.assertAlmostEqual(result["camera"]["intrinsics"]["fx"], 30 * 640 / 20.955)
        self.world.apply({"type": "update_camera", "camera_id": "Forward", "legacy": {"target": [1.2, 10, 1.0]}, "checks": {}})
        forward = wc.optical_forward(self.world.ops.cameras["/cams/Forward"]["pose"]["orientation_xyzw"])
        np.testing.assert_allclose(forward, [0, 1, 0], atol=1e-9)

    def test_invalid_camera_aim_leaves_state_unchanged(self):
        before = copy.deepcopy(self.world.config["cameras"])
        updates = dict(self.world.ops.cameras)
        with self.assertRaises(CommandError):
            self.world.apply({"type": "update_camera", "camera_id": "Mast", "legacy": {"target": [-0.6, 0, -5]}, "checks": {}})
        with self.assertRaises(CommandError) as raised:
            self.world.apply({"type": "update_camera", "camera_id": "Stern", "legacy": {"focal_length": 20}, "checks": {}})
        self.assertEqual(raised.exception.code, "unknown_camera")
        self.assertEqual(self.world.config["cameras"], before)
        self.assertEqual(self.world.ops.cameras, updates)

    def test_viewer_camera_is_separate_from_capture_cameras(self):
        self.world.apply({"type": "set_viewer_camera", "focal_length": 35.0})
        self.assertEqual(self.world.ops.viewer[-1]["focal_length"], 35.0)
        self.assertEqual(self.world.snapshot(True)["viewer"]["Overview"]["position"], [-10, -13, 10])

    def test_environment_current_feeds_hydrodynamics(self):
        self.world.apply({"type": "update_environment", "changes": {"current_mps": [0.5, 0, 0], "sun_intensity": 800.0}, "checks": {}})
        self.assertEqual(self.world.physics_config(self.world.config["entities"][0])["water"]["current"], [0.5, 0, 0])
        self.assertEqual((self.world.ops.lights[-1]["sun_intensity"], self.world.ops.lights[-1]["sky_intensity"]), (800.0, 700.0))

    def test_snapshot_is_json_ready_and_a_copy(self):
        state = self.world.snapshot(True)
        json.dumps(state)
        self.assertEqual((state["step_index"], state["sim_time_ns"], state["motion_mode"]), (0, 0, "internal"))
        self.assertIs(type(state["entities"]["boat"]["pose"]["position_m"][0]), float)
        state["cameras"]["Forward"]["resolution"][0] = 1
        self.assertEqual(self.world.config["cameras"][0]["resolution"][0], 640)
        self.world.detach()
        self.assertIsNone(self.world.snapshot(False)["entities"]["boat"])


class ExternalPoseWorld(unittest.TestCase):
    def setUp(self):
        self.world, self.body, self.ops = load_two_boats()

    def test_rows_map_by_prim_path_not_list_order(self):
        self.assertEqual(self.world.rows, {"marker": 0, "bravo": 1, "alpha": 2})
        np.testing.assert_allclose(self.body.transforms[1][:3], [10, 5, 0.3], atol=1e-6)

    def test_supplied_pose_survives_physics(self):
        self.world.apply({"type": "set_entity_pose", "entity_id": "alpha", "pose": POSE, "checks": {}})
        for _ in range(10):
            self.world.hold()
            self.body.physics_step()  # Pushes every body, the way gravity or contacts might.
            self.world.hold()
            self.world.advance()
        state = self.world.snapshot(True)["entities"]
        np.testing.assert_allclose(state["alpha"]["pose"]["position_m"], POSE["position_m"], atol=1e-6)
        np.testing.assert_allclose(state["bravo"]["pose"]["position_m"], [10, 5, 0.3], atol=1e-6)
        self.assertEqual(self.world.sim_time_ns, 10 * 5_000_000)

    def test_kinematic_bodies_get_no_velocity_writes(self):
        calls = []
        self.body.set_velocities = lambda *args: calls.append(args)
        self.world.apply({"type": "set_entity_pose", "entity_id": "alpha", "pose": POSE, "checks": {}})
        self.world.hold()
        self.assertEqual(calls, [])

    def test_stale_commands_are_refused(self):
        episode = self.world.episode_id
        checks = [
            ({"episode_id": "old"}, "stale_episode"),
            ({"expected_step": 3}, "stale_step"),
            ({"expected_revision": self.world.revision + 1}, "stale_revision"),
        ]
        for stale, code in checks:
            with self.subTest(code=code), self.assertRaises(CommandError) as raised:
                self.world.begin_step({"type": "step", "dt_ns": 20_000_000, "entity_updates": [], "checks": stale})
            self.assertEqual(raised.exception.code, code)
        self.world.begin_step({"type": "step", "dt_ns": 20_000_000, "entity_updates": [], "checks": {"episode_id": episode, "expected_step": 0, "expected_revision": self.world.revision}})

    def test_step_validates_everything_before_moving_anything(self):
        before = self.body.transforms.copy()
        bad = [
            ({"dt_ns": 21_000_000}, "inexact_time"),
            ({"camera_ids": ["ghost"]}, "unknown_camera"),
            ({"entity_updates": [{"entity_id": "alpha", "pose": POSE}, {"entity_id": "ghost", "pose": POSE}]}, "unknown_entity"),
        ]
        for change, code in bad:
            command = {"type": "step", "dt_ns": 20_000_000, "entity_updates": [], "camera_ids": None, "channels": ["rgb"], "checks": {}, **change}
            with self.subTest(code=code), self.assertRaises(CommandError) as raised:
                self.world.begin_step(command)
            self.assertEqual(raised.exception.code, code)
        np.testing.assert_array_equal(self.body.transforms, before)

    def test_capture_plan_rejects_unconfigured_channels(self):
        self.world.config["cameras"][1]["channels"] = ["rgb"]
        self.assertEqual([(c["id"], ch) for c, ch in self.world.capture_plan({"camera_ids": None, "channels": ["rgb"]})], [("bow", ["rgb"]), ("tower", ["rgb"])])
        with self.assertRaises(CommandError) as raised:
            self.world.capture_plan({"camera_ids": ["tower"], "channels": ["depth"]})
        self.assertEqual(raised.exception.code, "channel_not_configured")

    def test_camera_update_validates_against_the_camera(self):
        new_pose = {"position_m": [2, 0, 1], "orientation_xyzw": wc.look_at_orientation([2, 0, 1], [10, 0, 0])}
        result = self.world.apply({"type": "update_camera", "camera_id": "bow", "changes": {"pose": new_pose, "intrinsics": {"fx": 30, "fy": 30, "cx": 15, "cy": 12}}, "checks": {}})
        self.assertEqual(result["camera"]["pose"]["frame_id"], "alpha")
        self.assertAlmostEqual(result["camera"]["intrinsics"]["cx"], 15)
        for changes, code in (({"resolution": [64, 48]}, "unsupported_change"), ({"intrinsics": {"fx": 30, "fy": 30, "cx": 99, "cy": 1}}, "invalid_request"), ({"pose": {**new_pose, "frame_id": "world"}}, "invalid_request")):
            with self.subTest(changes=changes), self.assertRaises(CommandError) as raised:
                self.world.apply({"type": "update_camera", "camera_id": "bow", "changes": changes, "checks": {}})
            self.assertEqual(raised.exception.code, code)

    def test_reset_restores_scene_clock_and_starts_a_new_episode(self):
        episode = self.world.episode_id
        self.world.apply({"type": "set_entity_pose", "entity_id": "alpha", "pose": POSE, "checks": {}})
        self.world.apply({"type": "update_environment", "changes": {"sun_intensity": 5.0}, "checks": {}})
        self.world.apply({"type": "update_camera", "camera_id": "tower", "changes": {"clipping_range_m": [1, 2]}, "checks": {}})
        steps, _ = self.world.begin_step({"type": "step", "dt_ns": 20_000_000, "entity_updates": [], "checks": {}})
        for _ in range(steps):
            self.world.advance()
        self.world.end_step(steps)
        revision = self.world.revision
        with self.assertRaises(CommandError):
            self.world.apply({"type": "reset", "checks": {"expected_revision": revision - 1}})
        result = self.world.apply({"type": "reset", "checks": {"expected_revision": revision}, "seed": 11})
        self.assertNotEqual(result["episode_id"], episode)
        self.assertEqual((result["seed"], result["world_revision"]), (11, revision + 1))
        state = result["state"]
        self.assertEqual((state["step_index"], state["sim_time_ns"]), (0, 0))
        np.testing.assert_allclose(state["entities"]["alpha"]["pose"]["position_m"], [0, 0, 0.3], atol=1e-6)
        self.assertEqual(state["environment"]["sun_intensity"], 2500)
        self.assertEqual(state["cameras"]["tower"]["clipping_range_m"], [0.05, 250.0])
        self.assertEqual(self.world.held["alpha"][:3], [0, 0, 0.3])

    def test_frames_reference_downloads_with_effective_calibration_and_world_pose(self):
        from frames import FrameStore
        store = FrameStore()
        plan = self.world.capture_plan({"camera_ids": ["bow"], "channels": ["rgb", "depth"]})
        captured = {"bow": {"rgb": np.zeros((24, 32, 3), dtype=np.uint8), "depth": np.ones((24, 32), dtype=np.float32)}}
        self.assertEqual([c["id"] for c, _ in plan], ["bow"])
        observation = self.world.observation(20_000_000, captured, store)
        json.dumps(observation)
        self.assertEqual(observation["actual_dt_ns"], 20_000_000)
        rgb, depth = observation["frames"]
        self.assertEqual((rgb["channel"], rgb["encoding"], rgb["width"], rgb["height"]), ("rgb", "png", 32, 24))
        self.assertEqual((depth["encoding"], depth["url"]), ("f32le", f"/v1/frames/{depth['frame_id']}"))
        self.assertAlmostEqual(rgb["camera"]["intrinsics"]["fx"], 24)
        np.testing.assert_allclose(rgb["camera_world_pose"]["position_m"], [1.2, 0, 1.3], atol=1e-6)

    def test_runtime_fixed_settings_are_refused(self):
        catalog = wc.assets(load_config())
        for change, field in ((lambda w: w.update(physics_hz=100), "physics_hz"), (lambda w: w["environment"].update(water_level_m=1.0), "environment.water_level_m")):
            payload = two_boat_world()
            change(payload)
            with self.subTest(field=field), self.assertRaises(CommandError) as raised:
                self.world.check_runtime(wc.parse_world(payload, catalog))
            self.assertEqual((raised.exception.code, raised.exception.field), ("unsupported_change", field))


if __name__ == "__main__":
    unittest.main()
