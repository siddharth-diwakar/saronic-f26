"""Regression checks for rendering without automatic PhysX warmup."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from physics import attach_boat


class PhysicsStartup(unittest.TestCase):
    def test_render_then_attach_before_tensor_access_and_repeat_after_stop(self):
        events = []
        attached = False

        def initialize():
            nonlocal attached
            events.append("initialize")
            attached = True

        body = SimpleNamespace(count=1)
        view = Mock()
        view.create_rigid_body_view.return_value = body

        def create(backend, *, stage_id):
            self.assertTrue(attached, "Tensor access before PhysX stage attachment")
            self.assertEqual((backend, stage_id), ("numpy", 42))
            events.append("tensor")
            return view

        manager = SimpleNamespace(initialize_physics=initialize)
        rendering = SimpleNamespace(render=lambda: events.append("render"))
        tensors = SimpleNamespace(create_simulation_view=create)
        for _ in range(2):
            attached = False  # Stop detaches physics; Play must initialize again.
            self.assertEqual(attach_boat(manager, rendering, tensors, 42), (view, body))
        self.assertEqual(events, ["render", "initialize", "tensor"] * 2)
        view.set_subspace_roots.assert_called_with("/")
        view.create_rigid_body_view.assert_called_with("/World/Boat")

    def test_missing_boat_fails_before_readiness(self):
        view = Mock()
        view.create_rigid_body_view.return_value = SimpleNamespace(count=0)
        tensors = Mock()
        tensors.create_simulation_view.return_value = view
        with self.assertRaisesRegex(RuntimeError, "exactly one boat"):
            attach_boat(Mock(), Mock(), tensors, 42)


if __name__ == "__main__":
    unittest.main()
