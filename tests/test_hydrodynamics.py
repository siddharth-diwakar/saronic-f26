"""Physical invariants for the force model, runnable without a GPU."""
import copy
import math
import tempfile
import json
import unittest
from pathlib import Path
import numpy as np
from config import load_config
from hydrodynamics import forces, rotation_matrix, GRAVITY


class MarineForces(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.position = self.config["boat"]["position"]
        self.q = [0, 0, 0, 1]
        self.velocity = np.zeros(6)

    def test_equilibrium_displaces_boat_weight(self):
        force, torque = forces(self.config, self.position, self.q, self.velocity)
        np.testing.assert_allclose(force, [0, 0, self.config["boat"]["mass"]*GRAVITY], atol=1e-6)
        np.testing.assert_allclose(torque, 0, atol=1e-8)

    def test_dry_hull_has_no_lift_and_submerged_hull_has_bounded_lift(self):
        dry, _ = forces(self.config, [0, 0, 5], self.q, self.velocity)
        wet, _ = forces(self.config, [0, 0, -5], self.q, self.velocity)
        self.assertEqual(dry[2], 0)
        b = self.config["boat"]
        self.assertAlmostEqual(wet[2], 1000*GRAVITY*b["length"]*b["width"]*b["height"])

    def test_roll_and_pitch_have_restoring_torque(self):
        angle = math.radians(5)
        for axis in (0, 1):
            q = [0, 0, 0, math.cos(angle/2)]
            q[axis] = math.sin(angle/2)
            _, torque = forces(self.config, self.position, q, self.velocity)
            self.assertLess(torque[axis], 0)

    def test_drag_opposes_motion_and_tracks_current(self):
        velocity = np.array([2., -1., 0, 0, 0, .2])
        force, torque = forces(self.config, self.position, self.q, velocity)
        self.assertLess(np.dot(force[:2], velocity[:2]), 0)
        self.assertLess(torque[2]*velocity[5], 0)
        self.config["water"]["current"] = [2, -1, 0]
        force, _ = forces(self.config, self.position, self.q, velocity)
        np.testing.assert_allclose(force[:2], 0, atol=1e-9)

    def test_thrust_and_port_turn_signs(self):
        ahead, torque = forces(self.config, self.position, self.q, self.velocity, 1, 0)
        astern, _ = forces(self.config, self.position, self.q, self.velocity, -1, 0)
        self.assertAlmostEqual(ahead[0], 900)
        self.assertAlmostEqual(astern[0], -900)
        self.assertAlmostEqual(torque[2], 0)
        _, turn = forces(self.config, self.position, self.q, self.velocity, 0, 1)
        self.assertGreater(turn[2], 0)

    def test_thrust_follows_rotated_hull(self):
        q = [0, 0, math.sin(math.pi/4), math.cos(math.pi/4)]
        force, _ = forces(self.config, self.position, q, self.velocity, 1, 0)
        np.testing.assert_allclose(force[:2], [0, 900], atol=1e-8)
        np.testing.assert_allclose(rotation_matrix(q).T @ rotation_matrix(q), np.eye(3), atol=1e-8)

    def test_invalid_scenario_is_rejected(self):
        cases = []
        for section, key, value in [("boat", "mass", -1), ("boat", "mass", 10000), ("water", "level", float("nan")), ("simulation", "render_hz", 31), ("simulation", "seed", True)]:
            config = copy.deepcopy(self.config)
            config[section][key] = value
            cases.append(config)
        config = copy.deepcopy(self.config)
        config["cameras"][1]["name"] = "Forward"
        cases.append(config)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"invalid.json"
            for config in cases:
                path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
