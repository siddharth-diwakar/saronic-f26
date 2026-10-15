"""Runtime control API checks, runnable without Isaac Sim."""
import json
import math
import unittest
from http.client import HTTPConnection

from commands import CommandError, CommandQueue, check_look_at, parse_command, pose_transform, start_server

CAMERAS = ["Forward", "Mast"]


class ParseCommand(unittest.TestCase):
    def test_partial_updates_keep_only_named_fields(self):
        self.assertEqual(parse_command({"type": "set_controls", "throttle": 0.5}, CAMERAS), {"type": "set_controls", "throttle": 0.5})
        self.assertEqual(
            parse_command({"type": "set_camera", "name": "Overview", "focal_length": 35}, CAMERAS),
            {"type": "set_camera", "name": "Overview", "focal_length": 35.0},
        )
        self.assertEqual(parse_command({"type": "reset"}, CAMERAS), {"type": "reset"})

    def test_rejects_invalid_commands(self):
        invalid = [
            [],
            {"type": "explode"},
            {"type": "set_controls"},
            {"type": "set_controls", "throttle": 1.5},
            {"type": "set_controls", "throttle": True},
            {"type": "set_controls", "throttle": float("nan")},
            {"type": "set_controls", "throttle": 0, "speed": 3},
            {"type": "set_pose", "position": [1, 2]},
            {"type": "set_camera", "name": "Stern", "focal_length": 20},
            {"type": "set_camera", "name": "Forward"},
            {"type": "set_camera", "name": "Forward", "focal_length": 0},
            {"type": "set_environment", "sun_intensity": -1},
            {"type": "set_environment"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(CommandError):
                parse_command(payload, CAMERAS)

    def test_look_at_rejects_vertical_view_only(self):
        check_look_at([0, 0, 2], [5, 0, 0])
        with self.assertRaises(CommandError):
            check_look_at([0, 0, 10], [0, 0, 0])

    def test_pose_heading_sets_level_yaw_and_keeps_unspecified_position(self):
        current = [1, 2, 0.3, 0.1, 0.05, 0, 0.99]
        transform = pose_transform(current, heading=90)
        self.assertEqual(transform[:3], [1, 2, 0.3])
        self.assertAlmostEqual(transform[5], math.sin(math.pi / 4))
        self.assertAlmostEqual(transform[6], math.cos(math.pi / 4))
        self.assertEqual(transform[3:5], [0, 0])
        self.assertEqual(pose_transform(current, position=[5, 6, 0.3])[3:], current[3:])


class CommandQueueBehavior(unittest.TestCase):
    def test_results_track_queued_applied_and_failed(self):
        commands = CommandQueue()
        first = commands.submit({"type": "reset"})
        second = commands.submit({"type": "reset"})
        self.assertEqual([command_id for command_id, _ in commands.drain()], [first, second])
        self.assertEqual(list(commands.drain()), [])
        commands.complete(first)
        commands.complete(second, "Press Play before moving the boat")
        self.assertEqual(commands.result(first)["status"], "applied")
        self.assertEqual(commands.result(second), {"id": second, "status": "failed", "error": "Press Play before moving the boat"})

    def test_published_state_is_a_snapshot(self):
        commands = CommandQueue()
        state = {"controls": {"throttle": 0.0}}
        commands.publish(state)
        state["controls"]["throttle"] = 1.0
        self.assertEqual(commands.state()["controls"]["throttle"], 0.0)


class ControlServer(unittest.TestCase):
    def setUp(self):
        self.commands = CommandQueue()
        self.server = start_server(self.commands, CAMERAS, port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_binds_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_command_round_trip(self):
        status, body = self.request("POST", "/commands", {"type": "set_camera", "name": "Mast", "target": [9, 0, 0]})
        self.assertEqual((status, body["status"]), (202, "queued"))
        ((command_id, command),) = list(self.commands.drain())
        self.assertEqual(command_id, body["id"])
        self.assertEqual(command["target"], [9.0, 0.0, 0.0])
        self.commands.complete(command_id)
        self.assertEqual(self.request("GET", f"/commands/{command_id}"), (200, {"id": command_id, "status": "applied"}))

    def test_rejects_bad_requests_without_queueing(self):
        self.assertEqual(self.request("POST", "/commands", b"{not json")[0], 400)
        status, body = self.request("POST", "/commands", {"type": "set_controls", "steering": 4})
        self.assertEqual(status, 400)
        self.assertIn("steering", body["error"])
        self.assertEqual(self.request("POST", "/commands", b"x" * 70000)[0], 400)
        self.assertEqual(self.request("GET", "/commands/999")[0], 404)
        self.assertEqual(self.request("POST", "/other", {"type": "reset"})[0], 404)
        self.assertEqual(list(self.commands.drain()), [])

    def test_state_unavailable_until_published(self):
        self.assertEqual(self.request("GET", "/state")[0], 503)
        self.commands.publish({"time": 1.5})
        self.assertEqual(self.request("GET", "/state"), (200, {"time": 1.5}))


if __name__ == "__main__":
    unittest.main()
