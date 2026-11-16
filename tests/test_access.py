"""Bridge session auth and runtime identity, runnable without Isaac Sim."""
import json
import os
import tempfile
import unittest
from http.client import HTTPConnection

from access import RUNTIME_HEADER, Access, token_sha256
from commands import CommandQueue, start_server

CAMERAS = ["Forward", "Mast"]
TOKEN = "session-token-123"


class GuardedServer(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.session_path = os.path.join(directory.name, ".bridge-session.json")
        self.commands = CommandQueue()
        self.access = Access(self.session_path, runtime_id="runtime-a")
        self.server = start_server(self.commands, CAMERAS, port=0, access=self.access)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def write_session(self, session_id="session-1", token=TOKEN):
        # Same replace-by-rename the worker does over SSH.
        tmp = self.session_path + ".tmp"
        with open(tmp, "w") as file:
            json.dump({"session_id": session_id, "token_sha256": token_sha256(token)}, file)
        os.replace(tmp, self.session_path)

    def request(self, method, path, body=None, token=None, runtime=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if runtime is not None:
            headers[RUNTIME_HEADER] = runtime
        data = None if body is None else json.dumps(body).encode()
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), response.getheader(RUNTIME_HEADER)

    def test_open_until_a_session_file_appears(self):
        status, body, runtime = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], None)
        self.assertEqual((body["runtime_id"], runtime), ("runtime-a", "runtime-a"))
        self.assertEqual(self.request("POST", "/commands", {"type": "reset"})[0], 202)

    def test_session_token_required_once_provisioned(self):
        self.write_session()
        for token in (None, "wrong"):
            with self.subTest(token=token):
                status, body, runtime = self.request("POST", "/commands", {"type": "reset"}, token=token)
                self.assertEqual((status, body["error"]), (401, "unauthorized"))
                self.assertEqual(runtime, "runtime-a")
                self.assertEqual(self.request("GET", "/health", token=token)[0], 401)
        self.assertEqual(list(self.commands.drain()), [])
        status, body, _ = self.request("GET", "/health", token=TOKEN)
        self.assertEqual((status, body["session_id"], body["ready"]), (200, "session-1", False))
        self.assertEqual(self.request("POST", "/commands", {"type": "reset"}, token=TOKEN)[0], 202)

    def test_rotated_token_replaces_the_old_one(self):
        self.write_session(token="old")
        self.write_session(token="new")
        self.assertEqual(self.request("GET", "/health", token="old")[0], 401)
        self.assertEqual(self.request("GET", "/health", token="new")[0], 200)

    def test_never_reopens_after_the_session_file_disappears(self):
        self.write_session()
        self.assertEqual(self.request("GET", "/health", token=TOKEN)[0], 200)
        os.remove(self.session_path)
        status, body, _ = self.request("POST", "/commands", {"type": "reset"})
        self.assertEqual((status, body["error"]), (503, "session_unavailable"))
        self.assertEqual(list(self.commands.drain()), [])

    def test_malformed_session_file_refuses_requests(self):
        with open(self.session_path, "w") as file:
            file.write("{not json")
        self.assertEqual(self.request("GET", "/health")[0], 503)

    def test_stale_runtime_is_rejected_before_command_ids_are_read(self):
        self.write_session()
        submitted = self.request("POST", "/commands", {"type": "reset"}, token=TOKEN)[1]["id"]
        status, body, _ = self.request("GET", f"/commands/{submitted}", token=TOKEN, runtime="runtime-old")
        self.assertEqual((status, body), (409, {"error": "runtime_changed", "runtime_id": "runtime-a"}))
        status, body, _ = self.request("GET", f"/commands/{submitted}", token=TOKEN, runtime="runtime-a")
        self.assertEqual((status, body["status"]), (200, "queued"))

    def test_ready_once_the_simulation_loop_publishes_state(self):
        self.write_session()
        self.commands.publish({"time": 0.0})
        self.assertTrue(self.request("GET", "/health", token=TOKEN)[1]["ready"])
        self.assertEqual(self.request("GET", "/state", token=TOKEN)[:2], (200, {"time": 0.0}))

    def test_unguarded_server_keeps_existing_behaviour(self):
        server = start_server(CommandQueue(), CAMERAS, port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 404)
        self.assertIsNone(response.getheader(RUNTIME_HEADER))


class RuntimeIdentity(unittest.TestCase):
    def test_each_process_gets_a_distinct_runtime_id(self):
        self.assertNotEqual(Access("/nonexistent").runtime_id, Access("/nonexistent").runtime_id)


if __name__ == "__main__":
    unittest.main()
