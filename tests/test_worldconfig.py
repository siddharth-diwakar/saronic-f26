"""World configuration, exact time, and camera geometry, runnable without Isaac Sim."""
import copy
import math
import unittest

import numpy as np

import worldconfig as wc
from config import load_config
from errors import CommandError
from fakesim import two_boat_world


class WorldConfigValidation(unittest.TestCase):
    def setUp(self):
        self.catalog = wc.assets(load_config())

    def test_default_world_comes_from_the_scenario(self):
        world = wc.world_from_scenario(load_config())
        self.assertEqual([e["id"] for e in world["entities"]], ["boat"])
        self.assertEqual(world["entities"][0]["motion_type"], "dynamic")
        self.assertEqual([c["id"] for c in world["cameras"]], ["Forward", "Mast"])
        self.assertEqual(world["cameras"][0]["parent_entity_id"], "boat")
        self.assertEqual(world["physics_hz"], 200)

    def test_two_boat_world_normalizes(self):
        payload = two_boat_world()
        payload["entities"][0]["pose"]["orientation_xyzw"] = [0, 0, 0, 2]
        world = wc.parse_world(payload, self.catalog)
        self.assertEqual(world["entities"][0]["pose"], {"frame_id": "world", "position_m": [0.0, 0.0, 0.3], "orientation_xyzw": [0.0, 0.0, 0.0, 1.0]})
        self.assertEqual(world["cameras"][0]["pose"]["frame_id"], "alpha")
        self.assertEqual(world["cameras"][1]["pose"]["frame_id"], "world")

    def test_rejects_invalid_worlds_with_the_failing_field(self):
        cases = [
            (lambda w: w.update(physics_hz=120), "physics_hz", "inexact_time"),
            (lambda w: w.update(schema_version=2), "schema_version", "invalid_request"),
            (lambda w: w.update(extra=1), "extra", "unknown_field"),
            (lambda w: w["entities"].append(copy.deepcopy(w["entities"][0])), "entities[3].id", "invalid_request"),
            (lambda w: w["entities"][0].update(asset_id="submarine"), "entities[0].asset_id", "unknown_asset"),
            (lambda w: w["entities"][0].update(motion_type="dynamic"), "entities[0].motion_type", "invalid_request"),
            (lambda w: w["entities"][0].update(id="1boat"), "entities[0].id", "invalid_request"),
            (lambda w: w["entities"][0]["pose"].pop("orientation_xyzw"), "entities[0].pose.orientation_xyzw", "invalid_request"),
            (lambda w: w["entities"][0]["pose"].update(frame_id="alpha"), "entities[0].pose.frame_id", "invalid_request"),
            (lambda w: w["cameras"][0].update(parent_entity_id="ghost"), "cameras[0].parent_entity_id", "unknown_entity"),
            (lambda w: w["cameras"][0].update(channels=["thermal"]), "cameras[0].channels", "invalid_request"),
            (lambda w: w["cameras"][0]["intrinsics"].update(cx=99), "cameras[0].intrinsics.cx", "invalid_request"),
            (lambda w: w["cameras"][0].update(clipping_range_m=[5, 1]), "cameras[0].clipping_range_m", "invalid_request"),
            (lambda w: w["environment"].pop("sky_intensity"), "environment.sky_intensity", "invalid_request"),
        ]
        for mutate, field, code in cases:
            payload = two_boat_world()
            mutate(payload)
            with self.subTest(field=field), self.assertRaises(CommandError) as raised:
                wc.parse_world(payload, self.catalog)
            self.assertEqual((raised.exception.field, raised.exception.code), (field, code))

    def test_internal_mode_allows_dynamic_boats_only(self):
        world = wc.parse_world(two_boat_world("internal"), self.catalog)
        self.assertEqual([e["motion_type"] for e in world["entities"]], ["dynamic", "dynamic", "external"])
        payload = two_boat_world("internal")
        payload["entities"][2]["motion_type"] = "dynamic"
        with self.assertRaisesRegex(CommandError, "Only boats"):
            wc.parse_world(payload, self.catalog)

    def test_live_changes_reject_fields_fixed_at_world_configuration(self):
        with self.assertRaises(CommandError) as raised:
            wc.parse_environment({"water_level_m": 1}, partial=True)
        self.assertEqual(raised.exception.code, "unsupported_change")
        camera = wc.parse_world(two_boat_world(), self.catalog)["cameras"][0]
        for field in ("resolution", "channels", "parent_entity_id"):
            with self.subTest(field=field), self.assertRaises(CommandError) as raised:
                wc.parse_camera_update({field: None}, camera)
            self.assertEqual(raised.exception.code, "unsupported_change")


class ExactTime(unittest.TestCase):
    def test_periods_and_steps_are_exact_or_rejected(self):
        self.assertEqual(wc.period_ns(200), 5_000_000)
        self.assertEqual(wc.physics_steps(20_000_000, wc.period_ns(200)), 4)
        self.assertEqual(wc.physics_steps(20_000_000, wc.period_ns(100)), 2)
        with self.assertRaises(CommandError) as raised:
            wc.physics_steps(21_000_000, wc.period_ns(200))
        self.assertEqual(raised.exception.code, "inexact_time")
        with self.assertRaises(CommandError):
            wc.period_ns(120)  # 8333333.3 ns: 20 ms would silently round to 16.7 ms

    def test_decimal_seconds_convert_exactly(self):
        self.assertEqual(wc.seconds_to_ns(0.02), 20_000_000)
        self.assertEqual(wc.seconds_to_ns(0.1), 100_000_000)
        self.assertEqual(wc.seconds_to_ns(0), 0)
        with self.assertRaises(CommandError):
            wc.seconds_to_ns(1e-10)


class CameraGeometry(unittest.TestCase):
    def test_look_at_optical_frame_for_a_forward_camera(self):
        q = wc.look_at_orientation([0, 0, 1], [10, 0, 1])
        m = np.array(wc.quat_to_matrix(q))
        # Optical +Z along the view (+X), +X image right (starboard, -Y), +Y image down (-Z).
        np.testing.assert_allclose(m[:, 2], [1, 0, 0], atol=1e-9)
        np.testing.assert_allclose(m[:, 0], [0, -1, 0], atol=1e-9)
        np.testing.assert_allclose(m[:, 1], [0, 0, -1], atol=1e-9)
        with self.assertRaises(CommandError):
            wc.look_at_orientation([0, 0, 10], [0, 0, 0])

    def test_usd_camera_looks_down_its_negative_z(self):
        pose = {"position_m": [1, 2, 3], "orientation_xyzw": wc.look_at_orientation([1, 2, 3], [11, 2, 3])}
        rows = np.array(wc.usd_camera_matrix(pose))
        np.testing.assert_allclose(-rows[2, :3], [1, 0, 0], atol=1e-9)  # USD -Z is the view direction
        np.testing.assert_allclose(rows[1, :3], [0, 0, 1], atol=1e-9)  # USD +Y is image up
        np.testing.assert_allclose(rows[3], [1, 2, 3, 1])

    def test_intrinsics_round_trip_through_usd_optics(self):
        camera = {"resolution": [640, 360], "intrinsics": {"fx": 500.0, "fy": 480.0, "cx": 330.0, "cy": 170.0}, "clipping_range_m": [0.1, 100.0]}
        optics = wc.usd_optics(camera)
        self.assertAlmostEqual(optics["focal_length"], 500 * 20.955 / 640)
        for key, value in wc.intrinsics_from_optics(optics, camera["resolution"]).items():
            self.assertAlmostEqual(value, camera["intrinsics"][key], places=9)

    def test_legacy_focal_length_matches_the_old_camera(self):
        k = wc.focal_length_intrinsics(18.0, [640, 360])
        camera = {"resolution": [640, 360], "intrinsics": k, "clipping_range_m": [0.05, 250]}
        optics = wc.usd_optics(camera)
        self.assertAlmostEqual(optics["focal_length"], 18.0)
        self.assertAlmostEqual(optics["vertical_aperture"], 20.955 * 360 / 640)
        self.assertAlmostEqual(optics["horizontal_aperture_offset"], 0)

    def test_compose_pose_places_a_mounted_camera_in_the_world(self):
        parent = {"position_m": [10, 0, 0], "orientation_xyzw": wc.yaw_quaternion(90)}
        child = {"position_m": [1, 0, 2], "orientation_xyzw": [0, 0, 0, 1]}
        pose = wc.compose_pose(parent, child)
        np.testing.assert_allclose(pose["position_m"], [10, 1, 2], atol=1e-9)
        np.testing.assert_allclose(pose["orientation_xyzw"], wc.yaw_quaternion(90), atol=1e-9)

    def test_pose_transform_heading_levels_roll_and_pitch(self):
        current = [1, 2, 0.3, 0.1, 0.05, 0, 0.99]
        transform = wc.pose_transform(current, heading=90)
        self.assertEqual(transform[:3], [1, 2, 0.3])
        self.assertAlmostEqual(transform[5], math.sin(math.pi / 4))
        self.assertEqual(transform[3:5], [0, 0])
        self.assertEqual(wc.pose_transform(current, position=[5, 6, 0.3])[3:], current[3:])


if __name__ == "__main__":
    unittest.main()
