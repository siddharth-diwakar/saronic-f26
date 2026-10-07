"""Attach physics before accessing the boat's NumPy tensor view."""


def attach_boat(simulation_manager, rendering_manager, tensors, stage_id):
    # render() processes queued timeline events with playSimulations disabled.
    # The manager's PLAY callback can therefore skip its automatic warmup.
    rendering_manager.render()
    simulation_manager.initialize_physics()
    view = tensors.create_simulation_view("numpy", stage_id=stage_id)
    view.set_subspace_roots("/")
    body = view.create_rigid_body_view("/World/Boat")
    if body.count != 1:
        raise RuntimeError("Expected exactly one boat rigid body after physics initialization")
    return view, body
