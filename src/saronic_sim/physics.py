"""Attach physics before accessing the entities' NumPy tensor view."""
ENTITY_PATTERN = "/World/Entities/*"


def attach_entities(simulation_manager, rendering_manager, tensors, stage_id, count):
    """Return (simulation view, rigid body view, prim path per view row)."""
    # render() processes queued timeline events with playSimulations disabled.
    # The manager's PLAY callback can therefore skip its automatic warmup.
    rendering_manager.render()
    simulation_manager.initialize_physics()
    view = tensors.create_simulation_view("numpy", stage_id=stage_id)
    view.set_subspace_roots("/")
    body = view.create_rigid_body_view(ENTITY_PATTERN)
    if body.count != count:
        raise RuntimeError(f"Expected {count} entity rigid bodies after physics initialization, found {body.count}")
    # Row order follows the view, not the world config: map rows by prim path.
    return view, body, list(body.prim_paths)
