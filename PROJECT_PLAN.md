# Project plan: Isaac Sim water environment for Saronic

This plan is based on the project brief from Aditya (Saronic), the team's
planning notes, and what is already in this repository. Items not specified by
the brief are listed under [Open questions](#open-questions) rather than
assumed.

## Background (from the brief)

- Saronic has internal **models of boat inputs**: given a control input, how the
  boat will respond in 20 ms.
- Saronic builds **end-to-end sims**: given the boat at a specific location and
  angle, what do all of its cameras see in the environment? The goal is to
  **close the loop**: run the autonomy/control stack on those images and feed
  its control input into the boat physics model.
- Saronic has built this closed-loop sim internally and is looking at
  **NVIDIA Isaac Sim** to replace its internal camera rendering, because Isaac
  Sim is open source and well documented.
- Isaac Sim has road and underwater environments but **no water-surface
  environments**.

## Objective

Build a **water-surface environment in Isaac Sim** and expose an **API**,
similar to [OpenAI Gym](https://openai.com/index/openai-gym-beta/), that lets
Saronic:

1. **Place boats** in the environment.
2. **Place / mount cameras** (where a camera sits on the boat).
3. **Step forward in time**: pass in the boat's new position after Saronic's
   boat model has been applied.
4. **Render** what the cameras (the agent) see at that time.
5. **Drop many 3D models** into the environment and render what the cameras
   see.
6. Run **headlessly from a Python script**: spin up the environment, make API
   calls, and plug Saronic's stack into it.

In short: tell the environment where the boat is at time *t*; render what the
cameras see at time *t*.

The environment lives in its own repository (this one).

## Closed loop

```text
           ┌──────────────────────────────────────────────┐
           │  Saronic side                                │
           │  autonomy / control stack                    │
           │        │ control input                       │
           │        ▼                                     │
           │  boat model (dummy black box from Aditya)    │
           └────────┬─────────────────────────▲───────────┘
                    │ next boat position       │ camera images
                    ▼                          │
           ┌──────────────────────────────────────────────┐
           │  This project: Isaac Sim water environment   │
           │  set boat pose at t → render cameras at t    │
           └──────────────────────────────────────────────┘
```

Real boat data is harder to get. Per the brief, **Aditya can build a simple
dummy for the boat black box**: it takes camera inputs and returns the next
position. That is enough to build and test the loop end to end.

## What exists in the repo today

| Capability | Status | Where |
| --- | --- | --- |
| Isaac Sim 6.1.0 in Docker on a Brev GPU | Working | `Dockerfile`, `compose*.yaml`, `scripts/` |
| Brev Launchable that starts any branch (`SARONIC_REF`) and logs the commit | Working | `scripts/brev-bootstrap.sh`, `scripts/brev-setup.sh` |
| Procedural harbor: calm reflective water, dock, shore, buoys | Working | `src/saronic_sim/scene.py` |
| One boat with its own buoyancy, drag, and differential thrust model | Working | `src/saronic_sim/hydrodynamics.py`, `runtime.py` |
| Cameras configured in the boat frame (position, target, resolution, focal length) | Working | `scenarios/harbor.json`, `cameras.py` |
| Headless finite run that records RGB + depth per camera and a trajectory CSV | Working (CLI, constant throttle/steering) | `scripts/run.sh harbor --record` |
| Browser viewer with throttle/steering sliders and camera switching | Working | `compose.stream.yaml` |
| Optional visual-only boat USD model | Working | `--boat-usd` |
| Gym-style Python API (place, step with a given pose, render) | **Not started** | — |
| Multiple boats / many 3D models placed via API | **Not started** | — |
| Waves | **Not started** (calm water only) | — |
| Backend that starts and stops Brev GPUs on demand | **Not started** | team notes, P0 |

Note the difference from the brief: today the repo computes boat motion from
its own force model and throttle/steering. The brief's loop has **Saronic's
boat model** supply the next position, and the environment places the boat
there and renders. The API needs to support setting the pose directly.

## What P0 is

P0 is the smallest version that closes the loop described in the brief:

> A headless Python script spins up the water environment, places a boat and
> its cameras through the API, and repeatedly (1) sets the boat's position at
> time *t*, (2) renders what each camera sees at *t*, and (3) passes the images
> to Aditya's dummy black box to get the next position.

P0 is done when that script runs end to end on the GPU and saves the rendered
frames and the pose sequence.

**In P0:** water surface (the existing calm water is acceptable), one boat,
camera mounting, set-pose / step / render API, headless script, dummy black
box integration, and a way to get a GPU running it.

**After P0 (P1+):** many 3D models in the scene, multiple boats, waves and
wave-driven boat motion, visual fidelity work from Design, richer world-editing
endpoints.

## Workstreams

### 1. Water environment

- Start from the existing harbor scene and calm water surface.
- P1: waves. The team's notes propose a floater-point model: points on each
  hull, a Gerstner-wave water height evaluated at each point every physics
  step, buoyancy proportional to submersion depth applied at that point, plus
  linear and angular damping. The existing `hydrodynamics.forces` already
  samples eight hull points against a flat water level.
- Design iterates on the boat and water look in parallel (reference model in
  the team notes: USS Missouri on Sketchfab).

### 2. Placement API: boats, cameras, models

- Place a boat at a position and heading.
- Mount cameras on the boat: position, look-at target, and frame settings
  (focal length etc.), matching the "camera frame adjustment" and "camera
  look-at" items in the team notes.
- P1: place additional 3D models in the scene.

### 3. Gym-style step / render API

Mirroring the brief's description of Gym:

- **reset / setup**: build the environment, boat, and cameras.
- **step**: advance time and accept the boat's new position (computed by
  Saronic's boat model).
- **render**: return what each camera sees at the current time.

The repo's runtime loop already separates pose updates, physics/render
stepping, and camera capture (`runtime.py`, `cameras.py`), which is the
starting point for these calls.

### 4. Headless runner and dummy boat integration

- A Python script that starts the environment headlessly, calls the API, and
  runs the loop with Aditya's dummy black box.
- Agree on the black box's interface with Aditya (see open questions).

### 5. GPU infrastructure (team P0 notes)

From the team's notes: a small always-on CPU backend manages on-demand Brev
GPU instances, so Brev runs only during simulation sessions.

| Item from team notes | Owner (from notes) |
| --- | --- |
| Hosting choice for the backend (Apex server, Krish V's Proxmox, AWS, GCP, Heroku) | Tanush |
| Brev CLI: API-key login, deploy the existing Launchable by ID or create an instance and run the bootstrap script, branch selection with recorded commit | Swarit |
| Backend language/library (Rust, Node/TypeScript, Python/FastAPI); Postgres for session state; separate provisioning/cleanup worker | Jit |
| FastAPI scaffold, routes, Postgres models and schemas | Eddie |
| `POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/stop` | Swarit |
| Duplicate-instance prevention, startup/idle/max-duration timeouts, abandoned-instance detection after restart, verifying Brev actually terminated, saving recordings before deletion | Eddie |
| Instantiation and detail features: movement, camera frame adjustment, camera look-at, environment | Krish |
| Observability: structured logs tagged with session/instance/commit, session event timeline in Postgres, failure step + error + log tail, instance startup logs sent to backend | Krish |
| Testing and CI: fake Brev client behind one interface, state/timeout unit tests, integration tests against Postgres, GitHub Actions with lint, type-check, tests, shellcheck | Tanush |

Later (per notes): world-editing and object-lookup endpoints, streaming data
back to the client, Slack alerts.

## Open questions

The brief does not specify these; confirm with Aditya / Saronic:

1. **Black box interface:** input and output formats for the dummy (image
   format and resolution, pose representation, units, coordinate frame).
2. **Time step:** the boat models predict 20 ms ahead. Should each `step`
   advance 20 ms?
3. **Pose vs. physics:** should the environment only place the boat at the
   pose Saronic supplies, or also simulate water-driven motion (e.g. waves
   rolling the boat) on top of it?
4. **Cameras:** how many, where they are mounted, and their intrinsics on
   Saronic's boats.
5. **Outputs:** RGB only, or also depth / other sensors? (The repo records RGB
   and depth today.)
6. **3D models:** which boats and objects to drop into the scene, and where
   they come from.
7. **How Saronic runs it:** Python package calling a remote environment, or
   their stack running alongside the environment on the GPU machine.
8. **Success criteria:** what makes the rendered images good enough to replace
   their internal camera rendering.
