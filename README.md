# Saronic Isaac Sim starter

[Launch on NVIDIA Brev](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL)

Clone this repository onto a NVIDIA Brev **Linux GPU instance** and run a headless Isaac Sim scene inside Docker. The first scene places a visual placeholder boat at a requested world position and writes a USD file. It does not yet simulate buoyancy, propulsion, or sensors.

## Host requirements

- A GPU/driver supported by the pinned Isaac Sim image (currently `6.1.0`). Check the [Isaac Sim system requirements](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/requirements.html) before selecting the Brev instance.
- Docker Engine, Docker Compose plugin, and NVIDIA Container Toolkit configured for Docker GPU access. See [NVIDIA's container installation guide](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/install_container.html).
- Enough disk space for the large Isaac Sim image and its persistent caches.

## Run on Brev

The [Brev Launchable](https://brev.nvidia.com/launchable/deploy?launchableID=env-3KBxE5OmaRkasfoXEDUhXeFleBL) uses VM mode on one L40S GPU. Its setup script clones this feature branch, checks GPU access, builds the Isaac Sim container, and creates a smoke scene using the optional `BOAT_X` and `BOAT_Y` launch parameters (both default to `0`). The script is in [scripts/brev-setup.sh](scripts/brev-setup.sh). Creating the Launchable does not start a paid GPU instance; deployment happens when you select **Deploy Launchable**.

```bash
git clone https://github.com/siddharth-diwakar/saronic-f26.git
cd saronic-f26
./scripts/check-host.sh
./scripts/run.sh deploy boat --x 10 --y 5
```

The first run pulls and builds the pinned Isaac Sim image, so it takes longer. Later runs reuse the Docker image and named cache volumes. The scene is saved at `output/boat_scene.usd` on the host.

Isaac Sim runs as UID `1234` inside the container. `run.sh` grants that user access to `output/` using an ACL when `setfacl` is available; otherwise it makes only the generated-output directory writable by all local users. Install Ubuntu's `acl` package if you prefer ACLs.

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
- `scripts/check-host.sh`: quick Brev host checks.
- `scripts/run.sh`: build and run one scenario.
- `src/saronic_sim/cli.py`: scene generation entry point.
- `assets/`: USD models you add later.
- `output/`: generated scenes, ignored by Git.

The image is based on [NVIDIA's official Isaac Sim container](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/installation/install_container.html); the standalone script follows NVIDIA's [SimulationApp guidance](https://docs.isaacsim.omniverse.nvidia.com/6.1.0/python_scripting/manual_standalone_python.html).
