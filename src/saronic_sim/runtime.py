"""Run the harbor in one Isaac Sim process, including its browser viewport."""
import csv
import json
import time
import traceback
from pathlib import Path


def run(config, output, stream=False, duration=None, throttle=0, steering=0, record=False, boat_usd=None, control_port=None, lockstep=False):
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
    server = None
    try:
        import omni.physics.tensors as tensors
        import omni.kit.viewport.utility as viewport_utils
        import omni.timeline
        from isaacsim.core.simulation_manager import SimulationManager
        from isaacsim.core.rendering_manager import RenderingManager
        from scene import build_scene
        from hydrodynamics import forces
        from cameras import CameraRecorder, encode_frames
        from physics import attach_boat
        from commands import CommandError, CommandQueue, start_server
        from world import World
        from isaacsim.core.experimental.utils import stage as stage_utils
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
        sim_view, body = attach_boat(SimulationManager, RenderingManager, tensors, stage_utils.get_stage_id(stage))
        print("HarborPhysicsReady: USD stage attached; boat rigid body available", flush=True)
        indices = np.array([0], dtype=np.uint32)
        initial_transform = body.get_transforms().copy()
        controls = {"throttle": throttle, "steering": steering, "reset": False}
        window = None
        sliders = {}
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
                    sliders["throttle"] = throttle_slider
                    ui.Label("Steering (-1 starboard, +1 port)")
                    steering_slider = ui.FloatSlider(min=-1, max=1)
                    steering_slider.model.set_value(steering)
                    steering_slider.model.add_value_changed_fn(lambda m: controls.update(steering=m.as_float))
                    sliders["steering"] = steering_slider
                    with ui.HStack():
                        ui.Button("Overview", clicked_fn=lambda: select_camera("/World/Overview"))
                        for name, path in zip([c["name"] for c in config["cameras"]], paths):
                            ui.Button(name, clicked_fn=lambda p=path: select_camera(p))
                    ui.Button("Reset boat", clicked_fn=lambda: controls.update(reset=True))
                    ui.Label("Use the toolbar Play/Pause buttons to pause.")
            select_camera("/World/Overview")
        if record or lockstep:
            # Lockstep step commands return camera frames even when not recording.
            recorder = CameraRecorder(config, paths, run_output)
        world = World(stage, config, paths, controls, sliders, initial_transform)
        commands = None
        if control_port:
            commands = CommandQueue()
            server = start_server(commands, [c["name"] for c in config["cameras"]], control_port)
            print(f"HarborControl: listening on http://127.0.0.1:{control_port}", flush=True)
        # Warm up assets, render products, and streaming before marking readiness.
        for _ in range(20):
            RenderingManager.render()
        dt = 1/config["simulation"]["physics_hz"]
        render_every = int(config["simulation"]["physics_hz"]/config["simulation"]["render_hz"])
        elapsed, step, frame, next_record = 0.0, 0, 0, 0.0
        stopped = False
        wall_start = time.monotonic()
        pending = None  # Lockstep step in progress: {"id", "command", "steps", "remaining"}

        def log_command(command_id, command):
            # Replayable record of what changed during the run, in simulation time.
            with (run_output / "commands.jsonl").open("a") as log:
                log.write(json.dumps({"time": elapsed, "id": command_id, **command}) + "\n")

        def start_step(command_id, command):
            nonlocal pending
            if not lockstep:
                raise CommandError("step requires starting the harbor with --lockstep")
            if stopped:
                raise CommandError("Press Play before stepping")
            if "pose" in command:
                world.place(body, command["pose"], indices)
            steps = round(command["seconds"] / dt)
            pending = {"id": command_id, "command": command, "steps": steps, "remaining": steps}

        def finish_step():
            nonlocal pending
            command_id, command = pending["id"], pending["command"]
            # Replicator annotators can trail the viewport by a frame; render twice before reading.
            RenderingManager.render()
            RenderingManager.render()
            boat = (body.get_transforms()[0], body.get_velocities()[0])
            result = {"steps": pending["steps"], "seconds": pending["steps"] * dt, "state": world.snapshot(elapsed, True, boat)}
            if command["images"]:
                result["frames"] = encode_frames(recorder.read(), command["depth"])
            pending = None
            commands.complete(command_id, result=result)
            log_command(command_id, command)
            publish_state()

        def apply_commands():
            for command_id, command in commands.drain():
                try:
                    if command["type"] == "step":
                        start_step(command_id, command)
                        # Leave later commands queued until this step has rendered.
                        return
                    world.apply(command, None if stopped else body, indices)
                except CommandError as error:
                    commands.complete(command_id, str(error))
                    continue
                except Exception as error:
                    # A failed command must not stop a running simulation.
                    traceback.print_exc()
                    commands.complete(command_id, f"Internal error: {error}")
                    continue
                commands.complete(command_id)
                log_command(command_id, command)

        def publish_state():
            boat = None if stopped else (body.get_transforms()[0], body.get_velocities()[0])
            commands.publish(world.snapshot(elapsed, timeline.is_playing(), boat))

        with (run_output / "trajectory.csv").open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["time", "x", "y", "z", "qx", "qy", "qz", "qw", "vx", "vy", "vz", "wx", "wy", "wz", "throttle", "steering"])
            while app.is_running() and (duration is None or elapsed < duration):
                if stopped and timeline.is_playing():
                    sim_view, body = attach_boat(SimulationManager, RenderingManager, tensors, stage_utils.get_stage_id(stage))
                    stopped = False
                stopped = stopped or timeline.is_stopped()
                if pending and stopped:
                    commands.complete(pending["id"], "Simulation stopped during the step")
                    pending = None
                if commands and not pending:
                    apply_commands()
                if pending and pending["remaining"] == 0:
                    finish_step()
                    continue
                if not timeline.is_playing() or (lockstep and not pending and step > 0):
                    # Paused, or lockstep waiting for its next step: keep the viewer rendering.
                    RenderingManager.render()
                    if commands:
                        publish_state()
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
                if pending and not pending["command"]["physics"]:
                    # Kinematic step: hold the supplied pose instead of integrating forces.
                    world.place(body, pending["command"].get("pose", {}), indices)
                else:
                    force, torque = forces(config, transform[:3], transform[3:], velocity, controls["throttle"], controls["steering"])
                    body.apply_forces_and_torques_at_position(np.array([force], dtype=np.float32), np.array([torque], dtype=np.float32), None, indices, True)
                render = step % render_every == 0
                SimulationManager.step(update_fabric=SimulationManager.is_fabric_enabled())
                if render:
                    RenderingManager.render()
                    if commands:
                        publish_state()
                    if step == 0:
                        ready.write_text("Harbor runtime ready\n")
                        print(f"HarborReady: first physics step completed; output={run_output}", flush=True)
                elapsed += dt
                step += 1
                if pending:
                    pending["remaining"] -= 1
                if render and elapsed >= next_record:
                    transform = body.get_transforms()[0].copy()
                    velocity = body.get_velocities()[0].copy()
                    writer.writerow([elapsed, *transform, *velocity, controls["throttle"], controls["steering"]])
                    file.flush()
                    if recorder:
                        recorder.capture(frame)
                    frame += 1
                    next_record += 1/config["simulation"]["record_hz"]
                if stream and not lockstep:
                    remaining = wall_start + elapsed - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(remaining, dt))
        print(f"Harbor run finished: {elapsed:.2f}s; trajectory={run_output / 'trajectory.csv'}", flush=True)
    except Exception:
        # SimulationApp shutdown may take minutes; report the cause immediately.
        traceback.print_exc()
        raise
    finally:
        ready.unlink(missing_ok=True)
        if server:
            server.shutdown()
            server.server_close()
        if recorder:
            recorder.close()
        if timeline:
            timeline.stop()
        app.close()
