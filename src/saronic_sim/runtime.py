"""Run the harbor in one Isaac Sim process, including its browser viewport and control bridge."""
import csv
import json
import time
import traceback
from pathlib import Path


def run(config, output, stream=False, duration=None, throttle=0, steering=0, record=False, boat_usd=None, control_port=None, lockstep=False):
    from isaacsim import SimulationApp
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    ready = output / ".harbor-ready"
    ready.unlink(missing_ok=True)
    experience = "/isaac-sim/apps/isaacsim.exp.full.streaming.kit" if stream else ""
    # SimulationApp must exist before any Omniverse import.
    app = SimulationApp({"headless": True, "hide_ui": False if stream else True, "sync_loads": True}, experience=experience)
    recorder = None
    timeline = None
    server = None
    commands = None
    try:
        import omni.physics.tensors as tensors
        import omni.kit.viewport.utility as viewport_utils
        import omni.timeline
        from isaacsim.core.simulation_manager import SimulationManager
        from isaacsim.core.rendering_manager import RenderingManager
        from isaacsim.core.experimental.utils import stage as stage_utils
        import scene
        import worldconfig as wc
        from cameras import CameraRecorder
        from commands import CommandQueue, start_server
        from control import Controller
        from frames import FrameStore
        from hydrodynamics import forces
        from physics import attach_entities
        from world import World

        catalog = wc.assets(config)
        initial_world = wc.world_from_scenario(config)
        stage = scene.build_harbor(config)
        camera_paths = scene.build_world(stage, initial_world, catalog, boat_usd)
        # Startup markers (Harbor*: ...) are parsed by scripts/report-readiness.sh; keep them stable.
        print(f"HarborSceneLoaded: harbor stage built with {len(camera_paths)} boat cameras", flush=True)
        if not stage.GetRootLayer().Export(str(output / "harbor_scene.usda")):
            raise RuntimeError("Could not save the initial harbor scene")
        run_output = output / ("run_" + time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1000000:06d}")
        run_output.mkdir()
        (run_output / "scenario.json").write_text(json.dumps(config, indent=2)+"\n")
        (run_output / "run.json").write_text(json.dumps({"throttle": throttle, "steering": steering, "duration": duration, "record": record, "stream": stream, "lockstep": lockstep}, indent=2)+"\n")
        physics_hz = config["simulation"]["physics_hz"]
        SimulationManager.setup_simulation(dt=1/physics_hz, device="cpu")
        RenderingManager.set_dt(1/config["simulation"]["render_hz"])
        timeline = omni.timeline.get_timeline_interface()
        timeline.play()
        stage_id = stage_utils.get_stage_id(stage)

        def attach(world_config):
            return attach_entities(SimulationManager, RenderingManager, tensors, stage_id, len(world_config["entities"]))

        sim_view, body, prim_paths = attach(initial_world)
        print("HarborPhysicsReady: USD stage attached; entity rigid bodies available", flush=True)
        controls = {"throttle": throttle, "steering": steering, "reset": False}
        sliders = {}
        viewer = {"position": list(scene.OVERVIEW_POSITION), "target": list(scene.OVERVIEW_TARGET), "focal_length": scene.OVERVIEW_FOCAL_LENGTH}
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
                        for name, path in camera_paths.items():
                            ui.Button(name, clicked_fn=lambda p=path: select_camera(p))
                    ui.Button("Reset episode", clicked_fn=lambda: controls.update(reset=True))
                    ui.Label("Use the toolbar Play/Pause buttons to pause.")
            select_camera("/World/Overview")
        world = World(config, catalog, scene.UsdOps(stage), controls, sliders, viewer)
        frames = FrameStore()
        if control_port:
            commands = CommandQueue()
            frames.prefix = commands.runtime_id[:8] + "-"
            server = start_server(commands, control_port, catalog, frames)
            print(f"HarborControl: listening on http://127.0.0.1:{control_port} runtime={commands.runtime_id}", flush=True)
        if record or lockstep:
            recorder = CameraRecorder(initial_world["cameras"], camera_paths, run_output)
        # Warm up assets, render products, and streaming before marking readiness.
        probe = recorder or CameraRecorder(initial_world["cameras"], camera_paths, run_output)
        for _ in range(20):
            RenderingManager.render()
        # Fails startup if any camera renders nothing.
        names = list(probe.read())
        if probe is not recorder:
            probe.close()
        print(f"HarborCamerasReady: {', '.join(names)} rendered", flush=True)
        # One physics step proves the scene simulates; the episode then starts at time zero.
        SimulationManager.step(update_fabric=SimulationManager.is_fabric_enabled())
        world.load(initial_world, body, prim_paths, camera_paths)
        render_every = int(physics_hz / config["simulation"]["render_hz"])
        record_every = int(physics_hz / config["simulation"]["record_hz"])
        steps_total, frame = 0, 0
        duration_steps = None if duration is None else round(duration * physics_hz)
        stopped = False
        wall_start = time.monotonic()

        def log_command(command_id, command):
            # Replayable record of what changed during the run, in simulation time.
            with (run_output / "commands.jsonl").open("a") as log:
                log.write(json.dumps({"sim_time_ns": world.sim_time_ns, "episode_id": world.episode_id, "command_id": command_id, **command}) + "\n")

        def capture(plan):
            nonlocal recorder
            if recorder is None:
                # Render products need a few frames before their annotators hold data.
                recorder = CameraRecorder(world.config["cameras"], world.camera_paths, run_output)
                for _ in range(3):
                    RenderingManager.render()
            # Replicator annotators can trail the viewport by a frame; render twice before reading.
            RenderingManager.render()
            RenderingManager.render()
            return recorder.read(plan)

        def load_world(world_config):
            """Rebuild entities and cameras for PUT /world without restarting Isaac Sim."""
            nonlocal recorder, sim_view, body, prim_paths
            world.check_runtime(world_config)
            if recorder is not None:
                recorder.close()
                recorder = None
            world.detach()
            timeline.stop()
            RenderingManager.render()
            scene.remove_world(stage)
            paths = scene.build_world(stage, world_config, catalog, boat_usd)
            timeline.play()
            sim_view, body, prim_paths = attach(world_config)
            result = world.load(world_config, body, prim_paths, paths)
            if record or lockstep:
                recorder = CameraRecorder(world_config["cameras"], paths, run_output)
                for _ in range(3):
                    RenderingManager.render()
            return {**result, "entity_ids": [e["id"] for e in world_config["entities"]], "camera_ids": list(paths)}

        def on_ready(command_id):
            ready.write_text("Harbor runtime ready\n")
            print(f"HarborBridgeReady: probe command {command_id} applied; runtime={commands.runtime_id}", flush=True)

        controller = Controller(world, commands, frames, capture, load_world, lockstep, timeline.is_playing, log_command, on_ready) if commands else None

        if commands:
            commands.submit({"type": "probe"})

        trajectory = (run_output / "trajectory.csv").open("w", newline="")
        writer = csv.writer(trajectory)
        writer.writerow(["time", "entity_id", "x", "y", "z", "qx", "qy", "qz", "qw", "vx", "vy", "vz", "wx", "wy", "wz", "throttle", "steering"])
        while app.is_running() and (duration_steps is None or steps_total < duration_steps):
            if stopped and timeline.is_playing():
                sim_view, body, prim_paths = attach(world.config)
                world.attach(body, prim_paths)
                # A restarted timeline resets PhysX to the authored poses: start a new episode.
                world.reset()
                stopped = False
            if not stopped and timeline.is_stopped():
                stopped = True
                world.detach()
                if controller:
                    controller.abort_step("The timeline stopped during the step", "not_playing")
            if controls["reset"]:
                controls["reset"] = False
                if commands:
                    commands.submit({"type": "reset", "checks": {}})
                elif not stopped:
                    world.reset()
            if controller:
                controller.service()
                if controller.step_due:
                    controller.finish_step()
                    continue
            if not timeline.is_playing() or stopped or (controller and lockstep and controller.waiting):
                # Paused, or lockstep waiting for its next step: keep the viewer rendering.
                RenderingManager.render()
                if controller and controller.ready:
                    controller.publish()
                time.sleep(.01)
                wall_start = time.monotonic() - world.sim_time_ns / wc.NS_PER_S
                continue
            world.hold()
            world.apply_forces(forces)
            SimulationManager.step(update_fabric=SimulationManager.is_fabric_enabled())
            world.hold()
            world.advance()
            steps_total += 1
            if controller:
                controller.stepped()
            if steps_total % render_every == 0:
                RenderingManager.render()
                if controller and controller.ready:
                    controller.publish()
            if steps_total == 1:
                if not commands:
                    ready.write_text("Harbor runtime ready\n")
                print(f"HarborReady: first physics step completed; output={run_output}", flush=True)
            if steps_total % record_every == 0:
                seconds = world.sim_time_ns / wc.NS_PER_S
                for entity_id, state in world.entity_states().items():
                    pose = state["pose"]
                    writer.writerow([seconds, entity_id, *pose["position_m"], *pose["orientation_xyzw"], *state["linear_velocity_mps"], *state["angular_velocity_radps"], controls["throttle"], controls["steering"]])
                trajectory.flush()
                if record:
                    recorder.capture(frame)
                    frame += 1
            if stream and not lockstep:
                remaining = wall_start + world.sim_time_ns / wc.NS_PER_S - time.monotonic()
                if remaining > 0:
                    time.sleep(min(remaining, 1 / physics_hz))
        trajectory.close()
        print(f"Harbor run finished: {steps_total / physics_hz:.2f}s simulated; trajectory={run_output / 'trajectory.csv'}", flush=True)
    except Exception:
        # SimulationApp shutdown may take minutes; report the cause immediately.
        traceback.print_exc()
        raise
    finally:
        ready.unlink(missing_ok=True)
        if commands:
            # Callers learn the outcome instead of waiting on commands that will never apply.
            commands.fail_pending("The simulator is shutting down", code="runtime_stopped")
        if server:
            server.shutdown()
            server.server_close()
        if recorder:
            recorder.close()
        if timeline:
            timeline.stop()
        app.close()
