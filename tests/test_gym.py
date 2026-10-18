"""Lockstep client, step command, and closed-loop example against a fake simulation loop.

The HTTP server, command queue, World, and client are real; only Isaac Sim is
replaced: a thread applies commands the way runtime.py does in lockstep mode.
Needs NumPy and Pillow.
"""
import csv
import importlib.util
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from cameras import encode_frames
from commands import CommandError, CommandQueue, parse_command, start_server
from config import load_config
from saronic_env import HarborEnv, HarborError
from world import World

CAMERAS = ["Forward", "Mast"]
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "closed_loop.py"


class FakeBody:
    def __init__(self, transform):
        self.transforms = np.array([transform], dtype=np.float32)
        self.velocities = np.zeros((1, 6), dtype=np.float32)

    def get_transforms(self):
        return self.transforms.copy()

    def get_velocities(self):
        return self.velocities.copy()

    def set_transforms(self, transforms, indices):
        self.transforms = np.array(transforms, dtype=np.float32)

    def set_velocities(self, velocities, indices):
        self.velocities = np.array(velocities, dtype=np.float32)


class FakeLockstepSim:
    """Mirrors runtime.py's lockstep handling without Kit: physics_hz 50, frames carry the step count."""

    dt = 0.02

    def __init__(self):
        config = load_config()
        initial = [0.0, 0.0, 0.3, 0.0, 0.0, 0.0, 1.0]
        self.body = FakeBody(initial)
        self.world = World(Mock(), config, ["/a", "/b"], {"throttle": 0, "steering": 0, "reset": False}, None, np.array([initial], dtype=np.float32))
        self.world._update_camera = Mock()
        self.world._update_lights = Mock()
        self.commands = CommandQueue()
        self.elapsed, self.rendered = 0.0, 0
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def frames(self):
        color = np.full((4, 6, 3), self.rendered % 256, dtype=np.uint8)
        return {name: (color, np.full((4, 6), 7.5, dtype=np.float32)) for name in CAMERAS}

    def loop(self):
        indices = np.array([0], dtype=np.uint32)
        while self.running:
            for command_id, command in self.commands.drain():
                try:
                    if command["type"] != "step":
                        self.world.apply(command, self.body, indices)
                        self.commands.complete(command_id)
                        continue
                    if "pose" in command:
                        self.world.place(self.body, command["pose"], indices)
                    steps = round(command["seconds"] / self.dt)
                    self.elapsed += steps * self.dt
                    self.rendered += 1
                    boat = (self.body.get_transforms()[0], self.body.get_velocities()[0])
                    result = {"steps": steps, "seconds": steps * self.dt, "state": self.world.snapshot(self.elapsed, True, boat)}
                    if command["images"]:
                        result["frames"] = encode_frames(self.frames(), command["depth"])
                    self.commands.complete(command_id, result=result)
                except CommandError as error:
                    self.commands.complete(command_id, str(error))
            threading.Event().wait(0.002)


class LockstepClient(unittest.TestCase):
    def setUp(self):
        self.sim = FakeLockstepSim()
        self.server = start_server(self.sim.commands, CAMERAS, port=0)
        self.addCleanup(self.stop)
        self.env = HarborEnv(f"http://127.0.0.1:{self.server.server_address[1]}", timeout=5)

    def stop(self):
        self.sim.running = False
        self.server.shutdown()
        self.server.server_close()

    def test_step_places_boat_and_returns_decodable_frames(self):
        heading = 90
        observation = self.env.step(pose={"position": [4, -2, 0.3], "heading": heading}, seconds=0.1, physics=False, depth=True)
        self.assertAlmostEqual(observation.time, 0.1)
        np.testing.assert_allclose(observation.state["boat"]["position"], [4, -2, 0.3], atol=1e-6)
        np.testing.assert_allclose(observation.state["boat"]["orientation_xyzw"][2], math.sin(math.radians(heading) / 2), atol=1e-6)
        self.assertEqual(observation.cameras, CAMERAS)
        self.assertEqual(observation.rgb("Forward").shape, (4, 6, 3))
        np.testing.assert_array_equal(observation.depth("Mast"), 7.5)

    def test_render_reset_and_errors(self):
        self.env.step(pose={"position": [9, 9, 0.3]})
        observation = self.env.reset()
        self.assertEqual(observation.state["boat"]["position"][:2], [0.0, 0.0])
        self.assertEqual(observation.time, self.env.render().time)
        with self.assertRaises(HarborError):
            observation.depth("Forward")
        with self.assertRaisesRegex(HarborError, "HTTP 400"):
            self.env.step(seconds=-1)
        with self.assertRaisesRegex(HarborError, "HTTP 400"):
            self.env.set_camera("Stern", focal_length=20)
        self.env.set_camera("Forward", focal_length=20)
        self.assertEqual(self.sim.world.cameras["Forward"]["focal_length"], 20)

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
            self.assertEqual(len(list((Path(folder) / "Forward").glob("*.png"))), 5)


class StepCommand(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(
            parse_command({"type": "step"}, CAMERAS),
            {"type": "step", "seconds": 0.02, "physics": True, "images": True, "depth": False},
        )
        command = parse_command({"type": "step", "pose": {"orientation_xyzw": [0, 0, 0, 2]}}, CAMERAS)
        self.assertEqual(command["pose"]["orientation_xyzw"], [0, 0, 0, 1])
        for payload in (
            {"type": "step", "seconds": 61},
            {"type": "step", "pose": {}},
            {"type": "step", "pose": {"heading": 0, "orientation_xyzw": [0, 0, 0, 1]}},
            {"type": "step", "pose": {"velocity": [1, 0, 0]}},
            {"type": "step", "pose": {"orientation_xyzw": [0, 0, 0, 0]}},
            {"type": "step", "physics": "no"},
        ):
            with self.subTest(payload=payload), self.assertRaises(CommandError):
                parse_command(payload, CAMERAS)

    def test_wait_returns_after_completion_or_timeout(self):
        commands = CommandQueue()
        command_id = commands.submit({"type": "reset"})
        self.assertEqual(commands.wait(command_id, 0.01)["status"], "queued")
        threading.Timer(0.05, commands.complete, args=(command_id,), kwargs={"result": {"ok": True}}).start()
        self.assertEqual(commands.wait(command_id, 5), {"id": command_id, "status": "applied", "result": {"ok": True}})


if __name__ == "__main__":
    unittest.main()
