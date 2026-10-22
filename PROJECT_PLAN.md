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

Status as of `main` at `12a8e66` (through PR #10).

**Simulation (GPU instance)**

| Capability | Status | Where |
| --- | --- | --- |
| Isaac Sim 6.1.0 in Docker on a Brev GPU | Working | `Dockerfile`, `compose*.yaml`, `scripts/` |
| Brev Launchable that starts a branch, tag, or full commit SHA (`SARONIC_REF`) and logs the commit | Working | `scripts/brev-bootstrap.sh`, `scripts/brev-setup.sh` |
| Procedural harbor: calm reflective water, dock, shore, buoys | Working | `src/saronic_sim/scene.py` |
| One boat with its own buoyancy, drag, and differential thrust model | Working | `src/saronic_sim/hydrodynamics.py`, `runtime.py` |
| Cameras configured in the boat frame (position, target, resolution, focal length) | Working | `scenarios/harbor.json`, `cameras.py` |
| Headless finite run that records RGB + depth per camera and a trajectory CSV | Working (constant throttle/steering at start) | `scripts/run.sh harbor --record` |
| Browser viewer with throttle/steering sliders and camera switching | Working | `compose.stream.yaml` |
| Optional visual-only boat USD model | Working | `--boat-usd` |
| **Runtime control API** on a running sim: `set_controls`, `set_pose` (teleport, zeroes velocity), `reset`, `set_camera` (position, target, focal length), `set_environment` (current, sun, sky); `GET /state` returns time, boat pose/velocity, cameras, environment; applied commands logged to `commands.jsonl` | Working; localhost only, no auth, reached over SSH tunnel | `src/saronic_sim/commands.py`, `world.py`; `--control-port`, port `8212` in streaming mode |
| Returning rendered camera images through an API call | **Not on main** (images only written to disk with `--record`) | — |
| Lockstep stepping and Gym-style Python client with closed-loop example | **In progress, not merged** | branch `krish/gym-step` |
| Camera resolution change at runtime | Not supported (fixed at startup) | `README.md` |
| Multiple boats / many 3D models placed via API | **Not started** | — |
| Waves | **Not started** (calm water only) | — |

**Backend (CPU, `backend/`)**

| Capability | Status | Where |
| --- | --- | --- |
| FastAPI API + separate worker process, one image, Postgres 18, Alembic migrations, Coolify deployment guide | Working | `backend/README.md`, `Dockerfile`, `migrations/` |
| `POST /api/v1/sessions` (`ref` resolved to an exact commit SHA via `git ls-remote`), `GET /api/v1/sessions/{id}`, `POST /api/v1/sessions/{id}/stop` (idempotent) | Working | `sessions/router.py`, `refs.py` |
| Shared `X-API-Key` auth; `MAX_ACTIVE_SESSIONS` cap (default 1) | Working | `auth.py`, `config.py` |
| Session state machine `requested → provisioning → running → stopping → stopped` / `failed`; stop always **deletes** the Brev instance | Working | `sessions/transitions.py` |
| Session timeline `requested → … → ready` with instance-reported steps via `POST …/events`, capped startup logs via `POST …/logs`, Slack alert on reported failure | Backend side working; **nothing on the instance reports yet** | `sessions/timeline.py` |
| Brev CLI client (`create --launchable … --param`, `ls --json`, `stop`, `delete`) behind a `BrevClient` protocol, with a fake for tests | Working | `brev/client.py`, `tests/fakes.py` |
| Duplicate-instance prevention: instance names derived from session IDs, so a restarted worker finds existing instances instead of creating new ones | Working | `worker.py` |
| Brev spike script measuring real instance behaviour | Written, **not yet run** | `backend/scripts/brev_spike.py` |
| Startup / idle / maximum-duration timeouts | **Not implemented** (noted in `worker.py`) | — |
| Sweep for Brev instances with no matching session | **Not implemented** | — |
| Saving recordings off the instance before deletion | **Not implemented** | — |
| Instance-side stage reporting, ingest token, structured JSON logging | **In progress, not merged** | branch `krish/observability` |
| Backend handing out the sim's control API address to clients | **Not implemented** (control API is localhost-only) | — |
| GitHub Actions CI | **Not set up** (no `.github/`) | — |

Gap against the brief: `set_pose` now lets an external model place the boat,
but on `main` there is no call that advances time in lockstep and returns the
camera images, which is the step/render loop the brief describes. That is what
`krish/gym-step` is working toward.

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

Starting point on `main`: the runtime control API (`commands.py`, `world.py`)
already queues commands from an HTTP thread and applies them on the
simulation thread, and `set_pose` / `set_camera` / `GET /state` cover placement
and state. Missing: advancing time on request and returning camera frames.
In progress on `krish/gym-step`.

### 4. Headless runner and dummy boat integration

- A Python script that starts the environment headlessly, calls the API, and
  runs the loop with Aditya's dummy black box.
- Agree on the black box's interface with Aditya (see open questions).

### 5. GPU infrastructure (team P0 notes)

From the team's notes: a small always-on CPU backend manages on-demand Brev
GPU instances, so Brev runs only during simulation sessions.

| Item from team notes | Owner (from notes) | Status on `main` |
| --- | --- | --- |
| Hosting choice for the backend (Apex server, Krish V's Proxmox, AWS, GCP, Heroku) | Tanush | Coolify deployment documented in `backend/README.md` |
| Brev CLI: API-key login, deploy the existing Launchable by ID or create an instance and run the bootstrap script, branch selection with recorded commit | Swarit | Done: `create --launchable` with params; refs resolved to SHAs. Spike not yet run |
| Backend language/library (Rust, Node/TypeScript, Python/FastAPI); Postgres for session state; separate provisioning/cleanup worker | Jit | Decided: Python/FastAPI, Postgres 18, separate worker |
| FastAPI scaffold, routes, Postgres models and schemas | Eddie | Done |
| `POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/stop` | Swarit | Done (under `/api/v1`) |
| Duplicate-instance prevention, startup/idle/max-duration timeouts, abandoned-instance detection after restart, verifying Brev actually terminated, saving recordings before deletion | Eddie | Duplicate prevention, restart recovery, and delete-until-gone done; timeouts, orphan sweep, recording save not yet |
| Instantiation and detail features: movement, camera frame adjustment, camera look-at, environment | Krish | Done as the runtime control API |
| Observability: structured logs tagged with session/instance/commit, session event timeline in Postgres, failure step + error + log tail, instance startup logs sent to backend | Krish | Timeline, logs, and failure routes done on the backend; instance-side reporting and JSON logging on `krish/observability` |
| Testing and CI: fake Brev client behind one interface, state/timeout unit tests, integration tests against Postgres, GitHub Actions with lint, type-check, tests, shellcheck | Tanush | Fake client and tests done (Postgres tests opt-in via `TEST_DATABASE_URL`); no GitHub Actions yet |

Later (per notes): world-editing and object-lookup endpoints, streaming data
back to the client. Slack alerts are partly done (instance-reported failures
only).

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
