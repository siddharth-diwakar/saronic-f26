# Saronic Isaac Sim harbor

The CPU backend (session API that starts and stops Brev GPU instances on demand, its
worker, and the Coolify/PostgreSQL setup) is in [backend/](backend/README.md).
Deploy that directory independently of the GPU simulation environment below.

A Docker environment for NVIDIA Brev with a procedural harbor, floating boat,
differential thrust, two onboard cameras, and a browser viewer. Pinned to Isaac
Sim **6.1.0**. The default scene needs no external model downloads.

## Run this feature on an existing Brev instance

The [Brev Launchable](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL)
accepts a **SARONIC_REF** string parameter (a branch, tag or full 40-character
commit SHA), defaulting to `main`. Enter
`tanush/boat-harbor-simulation` to test this feature before it merges. After
merging, the default `main` starts the harbor too.

To update an existing GPU instance manually, open its terminal and run:

```bash
cd ~/saronic-f26
git fetch origin tanush/boat-harbor-simulation
git switch --detach origin/tanush/boat-harbor-simulation
export ISAACSIM_HOST="<current Brev public IP>"
./scripts/check-host.sh
./scripts/start-harbor.sh
```

Open `http://<current Brev public IP>:8210` in a Chromium browser. The harbor
loads and starts automatically. First startup can take several minutes while
Isaac Sim creates caches. The viewer waits for the harbor runtime to be ready.

In **Boat controls**:

- **Throttle:** 0 stops thrust; positive values move forward; negative values reverse.
- **Steering:** positive turns port (+Y from the initial heading); negative turns starboard.
- **Overview / Forward / Mast:** switch the viewport camera.
- **Reset boat:** restores its initial pose and clears its velocity; slider values persist.
- The normal Isaac Sim toolbar provides Play and Pause.

The ports remain TCP `8210`, TCP `49100`, and UDP `47998`, restricted to your IP
in Brev. Use the instance's current address; it can change after recreation.
Jupyter remains on the existing authenticated Brev link at port `8888`.

### Branch selection and startup

Brev supplies one source checkout at `~/saronic-f26`. The inline Launchable
script is [scripts/brev-bootstrap.sh](scripts/brev-bootstrap.sh): it fetches
`SARONIC_REF`, checks out that revision with a detached HEAD, logs the commit,
and executes that revision's `scripts/brev-setup.sh`. The setup script checks
the host, prepares containers, and starts the harbor; it does not switch
revisions or clone again. Local changes cause the bootstrap to refuse checkout.

Use a branch or tag, or `refs/heads/<branch>` / `refs/tags/<tag>` to disambiguate
names. Invalid or missing revisions fail startup. `SARONIC_REPO_DIR` optionally
overrides the checkout path for manual use. Older setup scripts may select their
own pinned revision; new feature branches should include this bootstrap refactor.

For manual branch selection on an existing instance:

```bash
SARONIC_REF=tanush/boat-harbor-simulation bash scripts/brev-bootstrap.sh
```

To configure a fresh Launchable, keep this repository as the Git source, add
`SARONIC_REF` with default `main`, and paste the exact bootstrap script into the
VM setup script. Future startup changes belong in the repository's setup script;
GPU, port, source, and parameter definitions remain Launchable settings.

The bootstrap retains a temporary placement-scene fallback for older revisions
without `scripts/brev-setup.sh`. A present setup script always takes precedence,
and its failures are reported. The fallback can be removed once the required
revisions all include the entry point.

Host requirements: supported NVIDIA GPU/driver with NVENC (such as L40S),
Docker Engine and Compose, NVIDIA Container Toolkit, and sufficient disk space
for the Isaac Sim image and persistent caches. See NVIDIA's
[requirements](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/requirements.html)
and [container guide](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/install_container.html).

## Scenarios and recording

Edit `scenarios/harbor.json` before starting. It controls hull dimensions and
mass, initial pose, thrust limits, drag, water level/current, simulation rates,
and camera positions, targets, resolution, and focal length. Units are meters,
kilograms, seconds, and newtons; yaw is in degrees. The world is Z-up, and the
boat's forward direction is local +X. Camera positions and targets are in the
boat frame. The default hull center is at its calculated calm-water equilibrium.
If mass or dimensions change, set initial Z to
`water.level + boat.height / 2 - boat.mass / (water.density * boat.length * boat.width)`.

`BOAT_X` and `BOAT_Y` override initial placement in streaming mode (both default
to zero). `HARBOR_SCENARIO` selects a mounted scenario path, for example
`/workspace/scenarios/harbor.json`. Restart to apply changes.

Run a finite experiment with camera recording:

```bash
# Stop the viewer's sim first to avoid running two GPU applications at once.
export ISAACSIM_HOST="<current Brev public IP>"
docker compose -f compose.yaml -f compose.stream.yaml stop web-viewer sim
./scripts/run.sh harbor --duration 20 --record --throttle 0.4 --steering 0.15
```

Each run writes `output/run_<timestamp>/` with:

- `scenario.json` and `run.json`: settings and initial controls.
- `trajectory.csv`: simulation time, boat pose, linear/angular velocity, and controls.
- `cameras/Forward/` and `cameras/Mast/`: RGB PNGs and ideal geometric depth NPYs
  in meters, recorded only with `--record`. Infinite depth can represent sky.

`output/harbor_scene.usda` saves the initial scene for inspection. Reopening the
USD alone displays the scene; the Python runtime supplies buoyancy and controls.
Restart the viewer with `./scripts/start-harbor.sh` after a finite experiment.

To use a custom appearance, place a **visual-only** USD model in `assets/` and
run `./scripts/run.sh harbor --boat-usd /workspace/assets/boat.usd --record`.
Its geometry should match the configured dimensions and its origin the hull
center. Nested rigid bodies are rejected. The configured box collision hull,
mass, inertia, and buoyancy approximation still define physical behavior.

The original placement-only command remains available:

```bash
./scripts/run.sh deploy boat --x 10 --y 5 --heading 90
```

## Control bridge

A running harbor serves an HTTP bridge inside the simulator process, so clients
change the live scene and read the result without restarting Isaac Sim. The
streaming viewer serves it on port `8212` (`HARBOR_CONTROL_PORT`); finite runs
enable it with `--control-port <port>`. It binds to **127.0.0.1 only**: the
backend's worker reaches it through an SSH tunnel; by hand, use
`ssh -L 8212:127.0.0.1:8212 <brev-instance>`. Do not open it publicly.

HTTP threads only validate and queue. The simulation thread applies queued
commands in order between physics steps, so the network never writes to the
scene. A `202` means **accepted, not applied**: poll `GET /v1/commands/{id}`, or
add `?wait=<seconds>` to block until the command is applied or failed.
`.harbor-ready` and `GET /health` report ready only after the simulation thread
has serviced a probe command against the loaded scene; until then every command
returns `503 not_ready`.

### Conventions

- Meters, seconds, Z-up world. A vessel's body frame: +X forward, +Y port, +Z up,
  origin at the hull center.
- `orientation_xyzw` quaternions, normalized on input. A pose is
  `{"frame_id", "position_m", "orientation_xyzw"}`; entity poses are in `world`.
- A camera pose places the **optical frame** in its parent entity's body frame (or
  `world` when `parent_entity_id` is null): +Z along the view, +X image right,
  +Y image down (OpenCV). Intrinsics `fx, fy, cx, cy` are pixels at the configured
  resolution; responses report the calibration the USD camera actually uses.
- Time is integer nanoseconds. `dt_ns` must be a whole number of physics periods;
  anything else is rejected with `inexact_time`, never rounded. The default
  scenario runs physics at 200 Hz, so a 20 ms step is exactly 4 physics steps.
- `world_revision` increases on every configuration change (world load, pose patch,
  camera, environment, reset). Send `episode_id`, `expected_step` and
  `expected_revision` to have stale commands refused (`stale_episode`,
  `stale_step`, `stale_revision`) instead of applied.
- Send an `Idempotency-Key` header on retries: a repeated key returns the first
  command instead of applying it again (`409 idempotency_conflict` if the body
  differs). The bundled client does this automatically.
- `runtime_id` (in `/health`, and accepted in bodies) changes when the simulator
  restarts. Command IDs restart too, so a mismatched `runtime_id` returns
  `409 runtime_changed`: earlier outcomes are unknown, not replayed.

### Routes

| Route | Body | Result |
| --- | --- | --- |
| `GET /health` | | `runtime_id`, `ready`, `episode_id` |
| `PUT /v1/world` | `WorldConfig`: `schema_version` 1, `seed`, `motion_mode` (`external` or `internal`), `physics_hz`, `entities[]`, `cameras[]`, `environment` | `entity_ids`, `camera_ids`, `episode_id`, `world_revision` |
| `PATCH /v1/entities/{id}/pose` | `pose` (full) | `entity_id`, applied `pose` read back from PhysX, `world_revision` |
| `PATCH /v1/cameras/{id}` | `pose`, `intrinsics`, `clipping_range_m` | effective `camera` |
| `PATCH /v1/environment` | `current_mps`, `sun_rotation`, `sun_intensity`, `sky_intensity` | `environment` |
| `POST /v1/step` | `dt_ns`, `entity_updates[{entity_id, pose}]`, `camera_ids` (default all), `channels` (`rgb`, `depth`) | Observation |
| `POST /v1/render` | `camera_ids`, `channels` | Observation, time unchanged |
| `POST /v1/reset` | optional `seed`, `expected_revision` | new `episode_id`, `state` |
| `GET /v1/state` | | State |
| `GET /v1/commands/{id}` | | `command_id`, `status` (`queued`, `running`, `applied`, `failed`), `applied_sim_time_ns`, `result` or `error` |
| `GET /v1/frames/{frame_id}` | | RGB as PNG; depth as little-endian float32, height x width, meters to the image plane |

Entities use the approved catalog (`harbor_boat`, `buoy_red`, `buoy_green`,
`marker_box`). `motion_type` `external` makes an entity kinematic: Isaac physics
never moves it, and its supplied pose is re-written around every physics step,
so the read-back pose is the supplied pose. `dynamic` boats (only in `internal`
mode) are driven by the approximate hydrodynamics model, the demo mode. Camera
`resolution`, `channels` and parent, `physics_hz`, and `water_level_m` are fixed
by the world or runtime; changing them live is rejected with `unsupported_change`.

State: `episode_id`, `step_index`, `sim_time_ns`, `world_revision`, `entities`
(pose, `linear_velocity_mps`, `angular_velocity_radps`), `cameras`, `environment`,
`controls`. An Observation adds `actual_dt_ns` and `frames[]`: `frame_id`,
`camera_id`, `channel`, `sim_time_ns`, `step_index`, `world_revision`, the
effective `camera` calibration and `camera_world_pose`, `encoding`, `width`,
`height`, and the `url` to download. Frames are kept in memory (512 MB, oldest
first).

Errors refused at the HTTP layer are `{"error": code, "message", "field"}`;
failed commands carry `"error": {"code", "message", "field"}`.

The harbor starts with the scenario as its world: one `dynamic` boat with id
`boat` and the `Forward` and `Mast` cameras. The original single-boat commands
still work on the first entity through `POST /commands`:

```bash
curl -s localhost:8212/commands -d '{"type": "set_controls", "throttle": 0.4, "steering": 0.1}'
curl -s localhost:8212/commands -d '{"type": "set_camera", "name": "Forward", "target": [12, 3, 0.5], "focal_length": 24}'
curl -s localhost:8212/commands -d '{"type": "set_pose", "position": [5, -3, 0.3], "heading": 90}'
curl -s localhost:8212/v1/commands/2   # queued, running, applied, or failed
```

`set_controls`, `set_pose` (partial pose, `heading` levels roll and pitch),
`reset`, `set_camera` (`Overview` is the viewer's camera), `set_environment`
(`current` in m/s) and `step` (`seconds`, `pose`, `physics`, `images`, `depth`)
keep their fields. Applied commands are appended, with simulation time and
episode, to the run's `commands.jsonl`.

## Gym-style stepping

For closed-loop experiments, start the harbor in **lockstep**: simulation time
advances only when a step arrives, and each step returns what the cameras see at
its boundary.

```bash
./scripts/run.sh harbor --lockstep --control-port 8212   # finite runs: stop the viewer first
```

`src/saronic_env` is a standard-library Python client (NumPy and Pillow only to
decode images):

```python
from saronic_env import HarborEnv

env = HarborEnv("http://127.0.0.1:8212")
env.load_world(world)                         # WorldConfig as above, motion_mode "external"
episode = env.reset_episode()["episode_id"]
observation = env.advance(
    20_000_000,                               # exactly 20 ms
    [{"entity_id": "alpha", "pose": {"position_m": [3, 0, 0.3], "orientation_xyzw": [0, 0, 0, 1]}}],
    channels=["rgb", "depth"],
    episode_id=episode, expected_step=0,
)
observation.rgb("bow")                        # HxWx3 uint8, downloaded on first use
observation.entity("alpha")["pose"]
```

The single-boat helpers remain: `env.step(pose=..., seconds=0.02, physics=False)`
holds a pose for one step on the default harbor boat, `env.render()` renders
without advancing time, and `env.reset()` starts a new episode.

`examples/closed_loop.py` is the P0 loop: a stand-in for Saronic's boat model
receives each step's images and pose, returns the next pose 20 ms later, and
the harbor renders it. Frames and `poses.csv` are written to `--output`.

## Verification

Without Isaac Sim, install NumPy and Pillow in your Python environment and run:

```bash
PYTHONPATH=src/saronic_sim:src python3 -m unittest discover -s tests -v
bash tests/test_brev_bootstrap.sh
bash -n scripts/*.sh
docker compose config --quiet
ISAACSIM_HOST=127.0.0.1 docker compose -f compose.yaml -f compose.stream.yaml config --quiet
```

On Brev, `./scripts/smoke-harbor.sh` runs idle and powered scenarios, verifies
flotation and forward/turning motion, and checks both cameras produced images
and depth. Stop the streaming containers first as shown above. Run this GPU
check before merging or switching the Launchable to this feature.

## Model scope

The water is a calm reflective surface. Eight displacement columns approximate
box-hull buoyancy and restoring torque; configurable body-frame linear and
quadratic drag and angular damping resist motion relative to current. Two
stern thrust forces provide propulsion and steering. The dock, shore, and
fixed buoys have collision geometry. This is a low-speed development model;
coefficients are illustrative and require calibration to a real vessel.

Waves, wakes, spray, wind, added mass, planing dynamics, noisy camera models,
ROS 2 publishing, and autonomy are subsequent work. Camera depth currently
represents scene geometry rather than the behavior of a physical depth sensor
looking at reflective water.

Runtime stepping follows NVIDIA's
[SimulationManager](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/py/source/extensions/isaacsim.core.simulation_manager/docs/index.html)
and [RenderingManager](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/py/source/extensions/isaacsim.core.rendering_manager/docs/index.html)
APIs. Camera output uses USD cameras and Replicator render products/annotators;
see [camera sensors](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/sensors/isaacsim_sensors_camera.html).

## Repo layout

- `src/saronic_sim/`: CLI, scenario validation, scene authoring, force model, runtime, cameras.
- `scenarios/`: versioned experiment settings.
- `scripts/`: branch-selection bootstrap, repository startup, host checks, volume permissions, and GPU smoke check.
- `compose*.yaml`, `Dockerfile`, `streaming/`: Docker and existing WebRTC viewer.
- `assets/`: optional models; `output/`: generated data, ignored by Git.

Isaac Sim runs as UID 1234. Preparation grants output access using ACLs when
available, otherwise makes the generated output directory writable locally;
named cache and settings volumes are prepared once for that user.
