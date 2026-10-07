# Saronic Isaac Sim starter

[Launch on NVIDIA Brev](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL)

Run a headless Isaac Sim scene inside Docker on a NVIDIA Brev **Linux GPU instance**. The first scene places a visual placeholder boat at a requested world position and writes a USD file. It does not yet simulate buoyancy, propulsion, or sensors.

## Host requirements

- A GPU/driver supported by the pinned Isaac Sim image (currently `6.1.0`). Check the [Isaac Sim system requirements](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/requirements.html) before selecting the Brev instance.
- Docker Engine, Docker Compose plugin, and NVIDIA Container Toolkit configured for Docker GPU access. See [NVIDIA's container installation guide](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/install_container.html).
- Enough disk space for the large Isaac Sim image and its persistent caches.

## Run on Brev

The [Brev Launchable](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL) uses VM mode on one L40S GPU. Brev checks out this repository at `~/saronic-f26`; the bootstrap selects `SARONIC_REF` (default `main`), then the selected revision’s setup script checks GPU access, builds the Isaac Sim container, gives its non-root user write access to persistent caches and settings, creates a smoke scene using the optional `BOAT_X` and `BOAT_Y` launch parameters (both default to `0`), and starts a browser viewer. The script is in [scripts/brev-setup.sh](scripts/brev-setup.sh). Creating the Launchable does not start a paid GPU instance; deployment happens when you select **Deploy Launchable**.

After the instance starts, open `http://<Brev-public-IP>:8210` in a Chromium-based browser. In the streamed Isaac Sim window, open `/workspace/output/boat_scene.usd` to view the deployed boat. The browser viewer needs TCP `8210` and `49100`, plus UDP `47998`, restricted to your IP in Brev. Jupyter remains available through Brev's authenticated secure link on port `8888`.

### Select a feature branch or release

The Launchable has a string parameter **SARONIC_REF**, defaulting to `main`.
At deployment, enter a branch such as `tanush/boat-harbor-simulation`, or a tag
such as `brev-v2`. Use `refs/heads/<branch>` or `refs/tags/<tag>` if both have the
same name. Invalid or missing revisions fail startup instead of falling back.

Brev supplies the source checkout at `~/saronic-f26`. The VM setup script is the
small bootstrap in [scripts/brev-bootstrap.sh](scripts/brev-bootstrap.sh): it
fetches the selected revision, checks it out with a detached HEAD, logs the
commit, and executes **that revision's** `scripts/brev-setup.sh`. It refuses to
replace a checkout with local changes. It does not clone the repository again.
The optional `SARONIC_REPO_DIR` overrides the checkout location for manual use.

For manual selection on an existing instance:

```bash
SARONIC_REF=tanush/boat-harbor-simulation bash scripts/brev-bootstrap.sh
```

Keep Docker setup, volume preparation, scene generation, and simulator startup
in `scripts/brev-setup.sh`. That script runs the selected checkout and must not
switch revisions. Future startup changes require a repo PR; the Launchable
bootstrap only needs editing if this entry-point contract changes. Brev GPU,
ports, Git source, and parameter definitions remain Launchable settings.

A temporary inline fallback handles revisions without `scripts/brev-setup.sh`: it
runs the existing host checks, placement scene generator, and streaming Compose
files from the selected checkout. It requires those starter files, preserves
`BOAT_X`/`BOAT_Y`, and does not change the selected revision. A present startup
script always takes precedence; an error in that script is not masked by the
fallback. Remove this fallback once the refactor and test branches include the
repo entry point.

The revision-selection refactor must be merged into `main` and included in
feature branches to guarantee this behavior. Older revisions may have setup
scripts that select their own pinned revision. In particular, the original
`brev-v2` startup selects `brev-v2` itself. Rebase new feature branches on the
refactor before using them through this bootstrap.

To configure a Launchable once: keep this repository as the Git source, add
`SARONIC_REF` as a string parameter with default `main`, and paste the exact
contents of `scripts/brev-bootstrap.sh` into its VM setup script. This inline
bootstrap can select a revision even when Brev's initial checkout predates the
bootstrap file. Afterward only the deployment parameter changes for a test.

Run the bootstrap's local Git-fixture checks with:

```bash
bash tests/test_brev_bootstrap.sh
```

For manual setup without the Launchable, clone the repository and run:

```bash
git clone https://github.com/siddharth-diwakar/saronic-f26.git
cd saronic-f26
./scripts/check-host.sh
./scripts/run.sh deploy boat --x 10 --y 5
```

The first run pulls and builds the pinned Isaac Sim image, so it takes longer. Later runs reuse the Docker image and named cache volumes. The scene is saved at `output/boat_scene.usd` on the host.

Isaac Sim runs as UID `1234` inside the container. `run.sh` grants that user access to `output/` using an ACL when `setfacl` is available; otherwise it makes only the generated-output directory writable by all local users. It also prepares the named cache, log, and settings volumes once so Isaac Sim can write to them. Install Ubuntu's `acl` package if you prefer ACLs.

To change the placement or heading:

```bash
./scripts/run.sh deploy boat --x -3 --y 8 --heading 90 --output /workspace/output/test_scene.usd
```

Coordinates are in meters in a Z-up world. `x` and `y` locate the center of the placeholder hull; `z` defaults to `0.25`, placing the hull bottom at the water surface. Heading is yaw in degrees about +Z. The water is a flat visual marker, not a fluid simulation.

To use your own USD boat model, place it in `assets/` and pass its path *inside the container*:

```bash
./scripts/run.sh deploy boat --x 10 --y 5 --boat-usd /workspace/assets/boat.usd
```

The model is referenced beneath `/World/Boat/Model`, so its authored origin and scale determine the final appearance. For portable saved scenes, keep the asset with the project and preserve its path when reopening the USD file.

## Repo layout

- `Dockerfile`, `compose.yaml`: pinned container runtime, GPU request, mounts, and persistent caches.
- `compose.stream.yaml`, `streaming/web-viewer/`: long-running Isaac Sim WebRTC server and browser viewer, based on NVIDIA's official viewer setup.
- `scripts/brev-bootstrap.sh`: minimal Launchable revision selector.
- `scripts/brev-setup.sh`: startup logic executed from the selected checkout.
- `scripts/check-host.sh`: quick Brev host checks.
- `scripts/run.sh`, `scripts/prepare-volumes.sh`: prepare writable volumes, then build and run one scenario.
- `src/saronic_sim/cli.py`: scene generation entry point.
- `assets/`: USD models you add later.
- `output/`: generated scenes, ignored by Git.

The image is based on [NVIDIA's official Isaac Sim container](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/install_container.html); the standalone script follows NVIDIA's [SimulationApp guidance](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/python_scripting/manual_standalone_python.html).
