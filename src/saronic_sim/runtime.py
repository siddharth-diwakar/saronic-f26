"""Run the harbor in one Isaac Sim process, including its browser viewport."""
import csv
import json
import time
from pathlib import Path


def run(config, output, stream=False, duration=None, throttle=0, steering=0, record=False, boat_usd=None):
    import numpy as np
    from isaacsim import SimulationApp
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    ready = output / ".harbor-ready"
    ready.unlink(missing_ok=True)
    experience = "/isaac-sim/apps/isaacsim.exp.full.streaming.kit" if stream else ""
    app = SimulationApp({"headless": True, "hide_ui": False if stream else True, "sync_loads": True}, experience=experience)
    recorder = None
    timeline = None
    try:
        import omni.physics.tensors as tensors
        import omni.kit.viewport.utility as viewport_utils
        import omni.timeline
        from isaacsim.core.simulation_manager import SimulationManager
        from isaacsim.core.rendering_manager import RenderingManager
        from scene import build_scene
        from hydrodynamics import forces
        from cameras import CameraRecorder
        np.random.seed(config["simulation"]["seed"])
        stage, paths = build_scene(config, boat_usd)
        if not stage.GetRootLayer().Export(str(output / "harbor_scene.usda")):
            raise RuntimeError("Could not save the initial harbor scene")
        run_output = output / ("run_" + time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1000000:06d}")
        run_output.mkdir()
        (run_output / "scenario.json").write_text(json.dumps(config, indent=2)+"\n")
        (run_output / "run.json").write_text(json.dumps({"throttle": throttle, "steering": steering, "duration": duration, "record": record, "stream": stream}, indent=2)+"\n")
        SimulationManager.setup_simulation(dt=1/config["simulation"]["physics_hz"], device="cpu")
        RenderingManager.set_dt(1/config["simulation"]["render_hz"])
        timeline = omni.timeline.get_timeline_interface()
        timeline.play()
        RenderingManager.render()
        sim_view = tensors.create_simulation_view("numpy")
        sim_view.set_subspace_roots("/")
        body = sim_view.create_rigid_body_view("/World/Boat")
        if body.count != 1:
            raise RuntimeError("Expected exactly one boat rigid body")
        indices = np.array([0], dtype=np.uint32)
        initial_transform = body.get_transforms().copy()
        controls = {"throttle": throttle, "steering": steering, "reset": False}
        window = None
        if stream:
            import omni.ui as ui
            window = ui.Window("Boat controls", width=330, height=220)
            def select_camera(path):
                viewport = viewport_utils.get_active_viewport()
                if viewport:
                    viewport.camera_path = path
            with window.frame:
                with ui.VStack(spacing=6):
                    ui.Label("Throttle (-1 reverse, 0 idle, 1 forward)")
                    throttle_slider = ui.FloatSlider(min=-1, max=1)
                    throttle_slider.model.set_value(throttle)
                    throttle_slider.model.add_value_changed_fn(lambda m: controls.update(throttle=m.as_float))
                    ui.Label("Steering (-1 starboard, +1 port)")
                    steering_slider = ui.FloatSlider(min=-1, max=1)
                    steering_slider.model.set_value(steering)
                    steering_slider.model.add_value_changed_fn(lambda m: controls.update(steering=m.as_float))
                    with ui.HStack():
                        ui.Button("Overview", clicked_fn=lambda: select_camera("/World/Overview"))
                        for name, path in zip([c["name"] for c in config["cameras"]], paths):
                            ui.Button(name, clicked_fn=lambda p=path: select_camera(p))
                    ui.Button("Reset boat", clicked_fn=lambda: controls.update(reset=True))
                    ui.Label("Use the toolbar Play/Pause buttons to pause.")
            select_camera("/World/Overview")
        if record:
            recorder = CameraRecorder(config, paths, run_output)
        # Warm up assets, render products, and streaming before marking readiness.
        for _ in range(20):
            RenderingManager.render()
        ready.write_text("Harbor runtime ready\n")
        print(f"HarborReady: scenario loaded; output={run_output}", flush=True)
        dt = 1/config["simulation"]["physics_hz"]
        render_every = int(config["simulation"]["physics_hz"]/config["simulation"]["render_hz"])
        elapsed, step, frame, next_record = 0.0, 0, 0, 0.0
        stopped = False
        wall_start = time.monotonic()
        with (run_output / "trajectory.csv").open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["time", "x", "y", "z", "qx", "qy", "qz", "qw", "vx", "vy", "vz", "wx", "wy", "wz", "throttle", "steering"])
            while app.is_running() and (duration is None or elapsed < duration):
                if stopped and timeline.is_playing():
                    RenderingManager.render()
                    sim_view = tensors.create_simulation_view("numpy")
                    sim_view.set_subspace_roots("/")
                    body = sim_view.create_rigid_body_view("/World/Boat")
                    stopped = False
                if not timeline.is_playing():
                    stopped = stopped or timeline.is_stopped()
                    RenderingManager.render()
                    time.sleep(.01)
                    wall_start = time.monotonic() - elapsed
                    continue
                if controls["reset"]:
                    body.set_transforms(initial_transform, indices)
                    body.set_velocities(np.zeros((1, 6), dtype=np.float32), indices)
                    controls["reset"] = False
                transform = body.get_transforms()[0].copy()
                velocity = body.get_velocities()[0].copy()
                if not np.isfinite(transform).all() or not np.isfinite(velocity).all():
                    raise RuntimeError("Boat physics became non-finite")
                force, torque = forces(config, transform[:3], transform[3:], velocity, controls["throttle"], controls["steering"])
                body.apply_forces_and_torques_at_position(np.array([force], dtype=np.float32), np.array([torque], dtype=np.float32), None, indices, True)
                render = step % render_every == 0
                SimulationManager.step(update_fabric=SimulationManager.is_fabric_enabled())
                if render:
                    RenderingManager.render()
                elapsed += dt
                step += 1
                if render and elapsed >= next_record:
                    transform = body.get_transforms()[0].copy()
                    velocity = body.get_velocities()[0].copy()
                    writer.writerow([elapsed, *transform, *velocity, controls["throttle"], controls["steering"]])
                    file.flush()
                    if recorder:
                        recorder.capture(frame)
                    frame += 1
                    next_record += 1/config["simulation"]["record_hz"]
                if stream:
                    remaining = wall_start + elapsed - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(remaining, dt))
        print(f"Harbor run finished: {elapsed:.2f}s; trajectory={run_output / 'trajectory.csv'}", flush=True)
    finally:
        ready.unlink(missing_ok=True)
        if recorder:
            recorder.close()
        if timeline:
            timeline.stop()
        app.close()
