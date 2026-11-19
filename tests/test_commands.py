"""Control bridge request parsing, command queue, and HTTP server, runnable without Isaac Sim."""
import json
import threading
import unittest
from http.client import HTTPConnection

import worldconfig as wc
from commands import CommandQueue, parse_command, parse_request, start_server
from config import load_config
from errors import CommandError
from fakesim import two_boat_world
from frames import FrameStore

CATALOG = wc.assets(load_config())


class LegacyCommands(unittest.TestCase):
    """The original /commands bodies translate to typed commands acting on the first entity."""

    def test_partial_updates_keep_only_named_fields(self):
        self.assertEqual(parse_command({"type": "set_controls", "throttle": 0.5}), {"type": "set_controls", "throttle": 0.5})
        self.assertEqual(parse_command({"type": "set_pose", "heading": 90}), {"type": "set_entity_pose", "entity_id": None, "partial": {"heading": 90.0}, "checks": {}})
        self.assertEqual(parse_command({"type": "set_camera", "name": "Overview", "focal_length": 35}), {"type": "set_viewer_camera", "focal_length": 35.0})
        self.assertEqual(
            parse_command({"type": "set_camera", "name": "Mast", "target": [9, 0, 0]}),
            {"type": "update_camera", "camera_id": "Mast", "legacy": {"target": [9.0, 0.0, 0.0]}, "checks": {}},
        )
        self.assertEqual(parse_command({"type": "set_environment", "current": [0.2, 0, 0]})["changes"], {"current_mps": [0.2, 0.0, 0.0]})
        self.assertEqual(parse_command({"type": "reset"}), {"type": "reset", "checks": {}})

    def test_step_converts_seconds_to_exact_nanoseconds(self):
        command = parse_command({"type": "step", "pose": {"orientation_xyzw": [0, 0, 0, 2]}, "physics": False, "depth": True})
        self.assertEqual(command["dt_ns"], 20_000_000)
        self.assertEqual(command["entity_updates"], [{"entity_id": None, "partial": {"orientation_xyzw": [0.0, 0.0, 0.0, 1.0]}}])
        self.assertEqual((command["physics"], command["camera_ids"], command["channels"]), (False, None, ["rgb", "depth"]))
        self.assertEqual(parse_command({"type": "step", "images": False})["camera_ids"], [])

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
            {"type": "set_camera", "name": "Forward"},
            {"type": "set_camera", "name": "Forward", "focal_length": 0},
            {"type": "set_environment", "sun_intensity": -1},
            {"type": "set_environment"},
            {"type": "step", "seconds": 61},
            {"type": "step", "seconds": 1e-10},
            {"type": "step", "pose": {}},
            {"type": "step", "pose": {"heading": 0, "orientation_xyzw": [0, 0, 0, 1]}},
            {"type": "step", "pose": {"velocity": [1, 0, 0]}},
            {"type": "step", "pose": {"orientation_xyzw": [0, 0, 0, 0]}},
            {"type": "step", "physics": "no"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(CommandError):
                parse_command(payload)


class TypedRequests(unittest.TestCase):
    def test_routes_map_to_typed_commands(self):
        pose = {"position_m": [1, 2, 0.3], "orientation_xyzw": [0, 0, 0, 1]}
        self.assertEqual(parse_request("PUT", "/v1/world", two_boat_world(), CATALOG)["type"], "load_world")
        command = parse_request("PATCH", "/v1/entities/alpha/pose", {"pose": pose, "expected_revision": 3}, CATALOG)
        self.assertEqual(command, {"type": "set_entity_pose", "entity_id": "alpha", "pose": {"frame_id": "world", **pose}, "checks": {"expected_revision": 3}})
        self.assertEqual(parse_request("PATCH", "/v1/cameras/bow", {"clipping_range_m": [1, 9]}, CATALOG)["changes"], {"clipping_range_m": [1, 9]})
        self.assertEqual(parse_request("PATCH", "/v1/environment", {"sun_intensity": 10}, CATALOG)["changes"], {"sun_intensity": 10.0})
        step = parse_request("POST", "/v1/step", {
            "runtime_id": "r", "episode_id": "e", "expected_step": 0, "expected_revision": 1, "dt_ns": 20_000_000,
            "entity_updates": [{"entity_id": "alpha", "pose": pose}], "camera_ids": ["bow"], "channels": ["depth", "rgb"],
        }, CATALOG)
        self.assertEqual(step["checks"], {"episode_id": "e", "expected_step": 0, "expected_revision": 1})
        self.assertEqual((step["dt_ns"], step["camera_ids"], step["channels"]), (20_000_000, ["bow"], ["rgb", "depth"]))
        self.assertEqual(parse_request("POST", "/v1/render", {}, CATALOG), {"type": "render", "camera_ids": None, "channels": ["rgb"], "checks": {}})
        self.assertEqual(parse_request("POST", "/v1/reset", {"seed": 3, "expected_revision": 2}, CATALOG), {"type": "reset", "checks": {"expected_revision": 2}, "seed": 3})
        self.assertIsNone(parse_request("POST", "/v1/nothing", {}, CATALOG))
        self.assertIsNone(parse_request("GET", "/v1/world", {}, CATALOG))

    def test_rejects_invalid_requests_with_the_failing_field(self):
        pose = {"position_m": [1, 2, 0.3], "orientation_xyzw": [0, 0, 0, 1]}
        cases = [
            ("PATCH", "/v1/entities/alpha/pose", {"pose": {"position_m": [1, 2, 3]}}, "pose.orientation_xyzw"),
            ("PATCH", "/v1/entities/9x/pose", {"pose": pose}, "entity_id"),
            ("PATCH", "/v1/environment", {"water_level_m": 2}, "water_level_m"),
            ("PATCH", "/v1/environment", {"fog": 2}, "fog"),
            ("POST", "/v1/step", {}, "dt_ns"),
            ("POST", "/v1/step", {"dt_ns": 0}, "dt_ns"),
            ("POST", "/v1/step", {"dt_ns": 0.02}, "dt_ns"),
            ("POST", "/v1/step", {"dt_ns": 20_000_000, "entity_updates": [{"entity_id": "a", "pose": pose}] * 2}, "entity_updates[1].entity_id"),
            ("POST", "/v1/step", {"dt_ns": 20_000_000, "channels": ["infrared"]}, "channels"),
            ("POST", "/v1/step", {"dt_ns": 20_000_000, "expected_step": -1}, "expected_step"),
            ("POST", "/v1/reset", {"seed": -1}, "seed"),
        ]
        for method, path, body, field in cases:
            with self.subTest(path=path, body=body), self.assertRaises(CommandError) as raised:
                parse_request(method, path, body, CATALOG)
            self.assertEqual(raised.exception.field, field)


class CommandQueueBehavior(unittest.TestCase):
    def test_results_track_queued_running_applied_and_failed(self):
        commands = CommandQueue()
        first = commands.submit({"type": "reset"})
        second = commands.submit({"type": "reset"})
        self.assertEqual([command_id for command_id, _ in commands.drain()], [first, second])
        self.assertEqual(list(commands.drain()), [])
        commands.start(first)
        self.assertEqual(commands.result(first)["status"], "running")
        commands.complete(first, result={"ok": True}, sim_time_ns=5_000_000)
        commands.complete(second, CommandError("Press Play first", code="not_playing"), sim_time_ns=0)
        self.assertEqual(commands.result(first), {"command_id": first, "status": "applied", "applied_sim_time_ns": 5_000_000, "result": {"ok": True}})
        self.assertEqual(commands.result(second)["error"], {"code": "not_playing", "message": "Press Play first"})
        commands.complete(commands.submit({"type": "reset"}), "boom")
        self.assertEqual(commands.result(3)["error"]["code"], "internal_error")

    def test_idempotency_key_returns_the_first_command(self):
        commands = CommandQueue()
        first = commands.submit({"type": "step", "dt_ns": 20_000_000}, key="k1")
        self.assertEqual(commands.submit({"type": "step", "dt_ns": 20_000_000}, key="k1"), first)
        self.assertEqual(len(list(commands.drain())), 1)
        with self.assertRaises(CommandError) as raised:
            commands.submit({"type": "step", "dt_ns": 40_000_000}, key="k1")
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertNotEqual(commands.submit({"type": "step", "dt_ns": 20_000_000}, key="k2"), first)

    def test_fail_pending_reports_unapplied_commands(self):
        commands = CommandQueue()
        running = commands.submit({"type": "step"})
        queued = commands.submit({"type": "reset"})
        next(commands.drain())
        commands.start(running)
        commands.fail_pending("The simulator is shutting down")
        for command_id in (running, queued):
            self.assertEqual(commands.result(command_id)["status"], "failed")
            self.assertEqual(commands.result(command_id)["error"]["code"], "runtime_stopped")

    def test_state_hidden_until_ready_and_is_a_snapshot(self):
        commands = CommandQueue()
        state = {"controls": {"throttle": 0.0}}
        commands.publish(state)
        self.assertIsNone(commands.state())
        commands.mark_ready()
        state["controls"]["throttle"] = 1.0
        self.assertEqual(commands.state()["controls"]["throttle"], 0.0)

    def test_wait_returns_after_completion_or_timeout(self):
        commands = CommandQueue()
        command_id = commands.submit({"type": "reset"})
        self.assertEqual(commands.wait(command_id, 0.01)["status"], "queued")
        threading.Timer(0.05, commands.complete, args=(command_id,), kwargs={"result": {"ok": True}}).start()
        self.assertEqual(commands.wait(command_id, 5)["result"], {"ok": True})


class ControlServer(unittest.TestCase):
    def setUp(self):
        self.commands = CommandQueue(runtime_id="runtime-a")
        self.frames = FrameStore()
        self.server = start_server(self.commands, 0, CATALOG, self.frames)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, raw=False):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        connection.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        payload = response.read()
        return (response.status, payload, dict(response.getheaders())) if raw else (response.status, json.loads(payload))

    def test_binds_localhost_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_refuses_commands_until_ready(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"runtime_id": "runtime-a", "ready": False, "episode_id": None}))
        self.assertEqual(self.request("GET", "/v1/state")[0], 503)
        status, body = self.request("POST", "/commands", {"type": "reset"})
        self.assertEqual((status, body["error"]), (503, "not_ready"))
        self.assertEqual(list(self.commands.drain()), [])

    def test_command_round_trip(self):
        self.commands.mark_ready()
        status, body = self.request("PATCH", "/v1/entities/alpha/pose", {"pose": {"position_m": [1, 2, 0.3], "orientation_xyzw": [0, 0, 0, 1]}})
        self.assertEqual((status, body["status"]), (202, "queued"))
        ((command_id, command),) = list(self.commands.drain())
        self.assertEqual((command_id, command["entity_id"]), (body["command_id"], "alpha"))
        self.commands.complete(command_id, result={"pose": "ok"}, sim_time_ns=10)
        self.assertEqual(self.request("GET", f"/v1/commands/{command_id}")[1]["applied_sim_time_ns"], 10)
        self.assertEqual(self.request("GET", f"/commands/{command_id}")[1]["status"], "applied")

    def test_rejects_bad_requests_without_queueing(self):
        self.commands.mark_ready()
        self.assertEqual(self.request("POST", "/commands", b"{not json")[0], 400)
        status, body = self.request("POST", "/commands", {"type": "set_controls", "steering": 4})
        self.assertEqual((status, body["error"], body["field"]), (400, "invalid_request", "steering"))
        # Refused from the declared length, before the body is read.
        self.assertEqual(self.request("POST", "/commands", b"{}", {"Content-Length": "300000"})[0], 400)
        status, body = self.request("GET", "/v1/commands/999")
        self.assertEqual((status, body["error"]), (404, "unknown_command"))
        self.assertEqual(self.request("POST", "/other", {"type": "reset"})[0], 404)
        self.assertEqual(self.request("POST", "/v1/step?wait=abc", {"dt_ns": 1})[0], 400)
        status, body = self.request("POST", "/v1/step", {"dt_ns": 20_000_000, "runtime_id": "runtime-old"})
        self.assertEqual((status, body["error"], body["runtime_id"]), (409, "runtime_changed", "runtime-a"))
        self.assertEqual(list(self.commands.drain()), [])

    def test_idempotency_key_over_http(self):
        self.commands.mark_ready()
        headers = {"Idempotency-Key": "abc"}
        first = self.request("POST", "/v1/reset", {}, headers)[1]["command_id"]
        self.assertEqual(self.request("POST", "/v1/reset", {}, headers)[1]["command_id"], first)
        status, body = self.request("POST", "/v1/reset", {"seed": 4}, headers)
        self.assertEqual((status, body["error"]), (409, "idempotency_conflict"))
        self.assertEqual(len(list(self.commands.drain())), 1)

    def test_frames_download_as_binary(self):
        import numpy as np
        frame_id = self.frames.add("depth", np.full((2, 3), 1.5, dtype=np.float32))[0]
        status, data, headers = self.request("GET", f"/v1/frames/{frame_id}", raw=True)
        self.assertEqual((status, headers["Content-Type"], headers["X-Frame-Width"], headers["X-Frame-Height"]), (200, "application/octet-stream", "3", "2"))
        np.testing.assert_array_equal(np.frombuffer(data, dtype="<f4"), 1.5)
        rgb_id = self.frames.add("rgb", np.zeros((4, 5, 3), dtype=np.uint8))[0]
        for _ in range(2):  # Second download is served from the encoded cache.
            status, data, headers = self.request("GET", f"/v1/frames/{rgb_id}", raw=True)
            self.assertEqual((status, headers["Content-Type"], headers["X-Frame-Width"], data[:4]), (200, "image/png", "5", b"\x89PNG"))
        self.assertEqual(self.request("GET", "/v1/frames/nope")[0], 404)


class FrameEviction(unittest.TestCase):
    def test_oldest_frames_are_evicted_by_size(self):
        import numpy as np
        store = FrameStore(max_bytes=250)
        ids = [store.add("depth", np.zeros((5, 5), dtype=np.float32))[0] for _ in range(3)]  # 100 bytes each
        self.assertIsNone(store.get(ids[0]))
        self.assertIsNotNone(store.get(ids[2]))


if __name__ == "__main__":
    unittest.main()
