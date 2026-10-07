# Saronic Isaac Sim harbor

A Docker environment for NVIDIA Brev with a procedural harbor, floating boat,
differential thrust, two onboard cameras, and a browser viewer. Pinned to Isaac
Sim **6.1.0**. The default scene needs no external model downloads.

## Run this feature on an existing Brev instance

The existing [Brev Launchable](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL)
still selects the older `brev-v2` starter. To try this feature, open a terminal
on your GPU instance and run:

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

For a fresh Launchable, configure its Git source as this repository and its
setup command to run `bash ~/saronic-f26/scripts/brev-setup.sh` from this feature
revision. `SARONIC_REF` selects a tag or branch, defaulting to
`tanush/boat-harbor-simulation` during feature bring-up. Brev supplies the
checkout; the setup script fetches the revision without cloning again.

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

## Verification

Without Isaac Sim, install NumPy in your Python environment and run:

```bash
PYTHONPATH=src/saronic_sim python3 -m unittest discover -s tests -v
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
- `scripts/`: host checks, volume permissions, startup, and GPU smoke check.
- `compose*.yaml`, `Dockerfile`, `streaming/`: Docker and existing WebRTC viewer.
- `assets/`: optional models; `output/`: generated data, ignored by Git.

Isaac Sim runs as UID 1234. Preparation grants output access using ACLs when
available, otherwise makes the generated output directory writable locally;
named cache and settings volumes are prepared once for that user.
