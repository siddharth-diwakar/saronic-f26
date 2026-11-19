"""End-to-end bridge behavior over HTTP: the Stage 1 and Stage 2 acceptance checks.

The HTTP server, command queue, Controller, World, frame store and client are real; only
Isaac Sim is replaced (see fakesim.py). Needs NumPy and Pillow.
"""
import csv
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np

import worldconfig as wc
from fakesim import FakeSim, two_boat_world
from saronic_env import HarborEnv, HarborError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed_loop.py"
STEP_NS = 20_000_000


def wait_ready(env):
    for _ in range(500):
        if env.health()["ready"]:
            return
        time.sleep(0.005)
    raise AssertionError("bridge never became ready")


class Harness(unittest.TestCase):
    lockstep = True

    def setUp(self):
        self.sim = FakeSim(lockstep=self.lockstep)
        self.addCleanup(self.sim.stop)
        self.env = HarborEnv(self.sim.url, timeout=5)
        wait_ready(self.env)

    def http(self, method, path, body=None):
        request = Request(self.sim.url + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())


    def http_error(self, method, path, body=None):
        with self.assertRaises(HTTPError) as raised:
            self.http(method, path, body)
        with raised.exception as error:
            return error.code, json.loads(error.read())["error"]


class StageOneInteractivity(Harness):
    """Move the existing boat, poll its command, read the position back; repeat without restarting."""

    lockstep = False

    def test_ready_only_after_a_command_was_serviced(self):
        health = self.env.health()
        self.assertEqual(health["runtime_id"], self.sim.commands.runtime_id)
        self.assertEqual(self.sim.commands.result(1)["status"], "applied")  # the startup probe
        self.assertTrue(health["episode_id"])

    def test_move_poll_and_read_back_repeatedly(self):
        for position in ([5.0, -3.0, 0.3], [-2.0, 4.0, 0.3], [0.5, 0.5, 0.3]):
            pose = {"position_m": position, "orientation_xyzw": wc.yaw_quaternion(45)}
            status, accepted = self.http("PATCH", "/v1/entities/boat/pose", {"pose": pose})
            self.assertEqual((status, accepted["status"]), (202, "queued"))
            for _ in range(500):
                status, result = self.http("GET", f"/v1/commands/{accepted['command_id']}")
                if result["status"] in ("applied", "failed"):
                    break
                time.sleep(0.002)
            self.assertEqual(result["status"], "applied")
            np.testing.assert_allclose(result["result"]["pose"]["position_m"], position, atol=1e-6)
            self.assertIsInstance(result["applied_sim_time_ns"], int)
            # The state snapshot reflects it too (dynamic boat in the demo world; read before it drifts far).
            state = self.env.state()
            self.assertEqual(state["world_revision"], result["result"]["world_revision"])

    def test_invalid_ids_and_runtime_loss_fail_clearly(self):
        with self.assertRaises(HarborError) as raised:
            self.env.place("ghost", [0, 0, 0], [0, 0, 0, 1])
        self.assertEqual(raised.exception.code, "unknown_entity")
        self.assertEqual(self.http_error("GET", "/v1/commands/99999"), (404, "unknown_command"))
        self.assertEqual(self.http_error("POST", "/v1/reset", {"runtime_id": "previous-runtime"}), (409, "runtime_changed"))
        with self.assertRaises(HarborError) as raised:
            self.env.advance(STEP_NS)
        self.assertEqual(raised.exception.code, "not_lockstep")


class StageTwoAcceptance(Harness):
    def setUp(self):
        super().setUp()
        loaded = self.env.load_world(two_boat_world())
        self.assertEqual((loaded["entity_ids"], loaded["camera_ids"]), (["alpha", "bravo", "marker"], ["bow", "tower"]))
        self.episode = self.env.reset_episode()["episode_id"]

    def test_hundred_exact_external_pose_steps(self):
        revision = self.env.state()["world_revision"]
        for step in range(100):
            alpha = {"position_m": [0.06 * (step + 1), 0.0, 0.3], "orientation_xyzw": wc.yaw_quaternion(step)}
            bravo = {"position_m": [10.0, 5.0 + 0.04 * (step + 1), 0.3], "orientation_xyzw": wc.yaw_quaternion(90)}
            observation = self.env.advance(
                STEP_NS,
                [{"entity_id": "alpha", "pose": alpha}, {"entity_id": "bravo", "pose": bravo}],
                channels=["rgb", "depth"],
                episode_id=self.episode, expected_step=step, expected_revision=revision,
            )
            data = observation.data
            self.assertEqual((data["step_index"], data["sim_time_ns"], data["actual_dt_ns"]), (step + 1, (step + 1) * STEP_NS, STEP_NS))
            for entity_id, pose in (("alpha", alpha), ("bravo", bravo)):
                actual = observation.entity(entity_id)["pose"]
                np.testing.assert_allclose(actual["position_m"], pose["position_m"], atol=1e-5)
                np.testing.assert_allclose(np.abs(np.dot(actual["orientation_xyzw"], pose["orientation_xyzw"])), 1, atol=1e-6)
            self.assertEqual(sorted((f["camera_id"], f["channel"]) for f in data["frames"]), [("bow", "depth"), ("bow", "rgb"), ("tower", "depth"), ("tower", "rgb")])
            self.assertTrue(all(f["sim_time_ns"] == data["sim_time_ns"] for f in data["frames"]))
        # The fake renders the step index into every pixel: frames come from the matching boundary.
        self.assertEqual(int(observation.rgb("bow")[0, 0, 0]), 100)
        np.testing.assert_array_equal(observation.depth("tower"), 7.5)
        # External obstacles never moved, although the fake physics pushed every body each step.
        np.testing.assert_allclose(observation.entity("marker")["pose"]["position_m"], [20, 0, 0.3], atol=1e-6)
        self.assertEqual(self.sim.physics_steps, 400)

    def test_retry_after_lost_response_does_not_advance_twice(self):
        real_open = HarborEnv._open
        lost = {"count": 0}

        def lose_first_response(env, method, path, body=None, timeout=None, key=None):
            response = real_open(env, method, path, body, timeout, key)
            if path.startswith("/v1/step") and lost["count"] == 0:
                lost["count"] += 1
                raise ConnectionResetError("response lost")
            return response

        with patch.object(HarborEnv, "_open", lose_first_response):
            observation = self.env.advance(STEP_NS, expected_step=0)
        self.assertEqual(lost["count"], 1)
        self.assertEqual((observation.data["step_index"], observation.data["sim_time_ns"]), (1, STEP_NS))
        self.assertEqual(self.env.state()["sim_time_ns"], STEP_NS)
        # A blind replay without the key is caught by the step check instead.
        with self.assertRaises(HarborError) as raised:
            self.env.advance(STEP_NS, expected_step=0)
        self.assertEqual(raised.exception.code, "stale_step")

    def test_reset_restores_initial_scene_and_time(self):
        self.env.place("alpha", [9, 9, 0.3], [0, 0, 0, 1])
        self.env.update_environment(sun_intensity=10)
        self.env.advance(STEP_NS * 3)
        reset = self.env.reset_episode(seed=5)
        self.assertNotEqual(reset["episode_id"], self.episode)
        state = self.env.state()
        self.assertEqual((state["episode_id"], state["step_index"], state["sim_time_ns"]), (reset["episode_id"], 0, 0))
        np.testing.assert_allclose(state["entities"]["alpha"]["pose"]["position_m"], [0, 0, 0.3], atol=1e-6)
        self.assertEqual(state["environment"]["sun_intensity"], 2500)

    def test_render_does_not_advance_time(self):
        self.env.advance(STEP_NS)
        observation = self.env.render_cameras(["tower"])
        self.assertEqual((observation.data["sim_time_ns"], observation.data["actual_dt_ns"], observation.cameras), (STEP_NS, 0, ["tower"]))

    def test_inexact_durations_are_rejected_not_rounded(self):
        with self.assertRaises(HarborError) as raised:
            self.env.advance(STEP_NS + 1)
        self.assertEqual(raised.exception.code, "inexact_time")
        self.assertEqual(self.env.state()["sim_time_ns"], 0)

    def test_live_camera_and_environment_changes(self):
        result = self.env.update_camera("tower", intrinsics={"fx": 30, "fy": 28, "cx": 15, "cy": 11})
        self.assertAlmostEqual(result["camera"]["intrinsics"]["fy"], 28)
        with self.assertRaises(HarborError) as raised:
            self.env.update_camera("tower", resolution=[64, 48])
        self.assertEqual(raised.exception.code, "unsupported_change")
        with self.assertRaises(HarborError) as raised:
            self.env.update_environment(water_level_m=3)
        self.assertEqual(raised.exception.code, "unsupported_change")

    def test_world_reload_keeps_the_runtime(self):
        runtime = self.env.health()["runtime_id"]
        world = two_boat_world()
        world["entities"] = world["entities"][:1]
        world["cameras"] = world["cameras"][:1]
        loaded = self.env.load_world(world)
        self.assertEqual((loaded["entity_ids"], self.sim.loads), (["alpha"], 2))
        self.assertEqual(self.env.health()["runtime_id"], runtime)
        with self.assertRaises(HarborError):
            self.env.place("bravo", [0, 0, 0], [0, 0, 0, 1])


class SingleBoatClient(Harness):
    """The original Gym-style interface on the default harbor world."""

    def test_step_places_boat_and_returns_decodable_frames(self):
        observation = self.env.step(pose={"position": [4, -2, 0.3], "heading": 90}, seconds=0.1, physics=False, depth=True)
        self.assertAlmostEqual(observation.time, 0.1)
        boat = observation.entity()["pose"]
        np.testing.assert_allclose(boat["position_m"], [4, -2, 0.3], atol=1e-6)
        self.assertEqual(observation.cameras, ["Forward", "Mast"])
        self.assertEqual(observation.rgb("Forward").shape, (360, 640, 3))
        np.testing.assert_array_equal(observation.depth("Mast"), 7.5)

    def test_render_reset_and_errors(self):
        self.env.step(pose={"position": [9, 9, 0.3]})
        observation = self.env.reset()
        self.assertEqual(observation.entity()["pose"]["position_m"][:2], [0.0, 0.0])
        self.assertEqual(observation.time, 0)
        self.assertEqual(self.env.render().time, 0)
        with self.assertRaises(HarborError):
            observation.depth("Forward")
        with self.assertRaisesRegex(HarborError, "HTTP 400"):
            self.env.step(seconds=-1)
        with self.assertRaises(HarborError) as raised:
            self.env.set_camera("Stern", focal_length=20)
        self.assertEqual(raised.exception.code, "unknown_camera")
        result = self.env.set_camera("Forward", focal_length=20)
        self.assertAlmostEqual(result["camera"]["intrinsics"]["fx"], 20 * 640 / 20.955)

    def test_closed_loop_example_runs_end_to_end(self):
        spec = importlib.util.spec_from_file_location("closed_loop", EXAMPLE)
        example = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(example)
        with tempfile.TemporaryDirectory() as folder:
            argv = ["closed_loop.py", "--url", self.env.url, "--steps", "5", "--output", folder]
            with patch.object(sys, "argv", argv):
                example.main()
            with open(Path(folder) / "poses.csv") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 5)
            # The dummy model drives forward at 3 m/s: x advances about 6 cm per 20 ms step.
            self.assertAlmostEqual(float(rows[1]["x"]) - float(rows[0]["x"]), 0.06, places=3)
            self.assertAlmostEqual(float(rows[1]["time"]) - float(rows[0]["time"]), 0.02, places=9)
            self.assertEqual(len(list((Path(folder) / "Forward").glob("*.png"))), 5)


if __name__ == "__main__":
    unittest.main()
