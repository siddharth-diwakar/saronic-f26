# Backend infrastructure

CPU-only FastAPI backend, deployed independently of Isaac Sim. It starts and
stops on-demand Brev GPU instances for simulation sessions:

```text
client ──HTTPS──> API (FastAPI) ──Postgres── worker ──brev CLI──> Brev GPU instance
                  /api/v1/sessions                    (Launchable → bootstrap → Isaac Sim)
```

Two processes share one image and one database:

- **API** (`uvicorn saronic_api.main:app`): session endpoints. It never calls
  Brev and needs no Brev credentials.
- **Worker** (`python -m saronic_api.worker`): the only process that calls Brev.
  It creates, polls and deletes instances in the background.

FastAPI's built-in `/docs`, `/redoc`, and `/openapi.json` are available; `/`
returns 404.

## Sessions API

All routes require the `X-API-Key` header (see `SARONIC_API_KEY` below).

| Route | Result |
| --- | --- |
| `POST /api/v1/sessions` with optional body `{"ref": "main"}` | **202** with the session. `ref` is a branch, tag, `refs/heads/<x>`, `refs/tags/<x>` or full 40-character SHA, and defaults to `SARONIC_DEFAULT_REF`. **422** for an invalid, ambiguous or unknown ref, **502** if GitHub is unreachable, **409** when `MAX_ACTIVE_SESSIONS` sessions are already active. |
| `GET /api/v1/sessions/{id}` | **200** with the session, its timeline and log excerpts, or **404**. |
| `POST /api/v1/sessions/{id}/stop` | **202** with the session, or **404**. Idempotent. |
| `POST /api/v1/sessions/{id}/events` | For the instance: report the next startup step (see [Timeline](#timeline)). **200**, **404**, **409** if it is not the next step or the session is no longer starting, **422** for an invalid step or failure. |
| `POST /api/v1/sessions/{id}/logs` with `{"source": "bootstrap", "body": "..."}` | For the instance: store a capped startup log (`bootstrap`, `docker_build`, `isaac_launch` or `failure_tail`). **200** or **404**. |

The ref is resolved to a commit when the session is created. The instance always
runs that exact commit (`SARONIC_REF=<sha>`), even if the branch moves later.

Session status goes `requested → provisioning → running → stopping → stopped`,
or ends in `failed` (with `error.code`). `running` means the Brev VM is up, **not**
that the sim is ready. The response's `brev` object carries Brev's raw `status`,
`build_status` and `health_status` from the worker's last poll. Stopping always
**deletes** the Brev instance (`brev stop` would keep billing for the disk), and
every failure path that may have created an instance also deletes it.

### Timeline

`status` is the worker's view of Brev. `step` and `timeline` record how far startup
got, one event per step with a timestamp, the instance and the commit:

```text
requested → provisioning → instance_up → container_built → scene_loaded
          → physics_running → cameras_ready → ready
```

plus `failed`, `teardown_requested` and `stopped`. The backend records `requested`
through `instance_up` and the teardown steps as `status` changes. The instance
reports `container_built` through `ready` with `POST /events {"step": "..."}`, one
step at a time. A step that is already recorded is a no-op, so reports can be retried.
`container_built` arriving before the worker has seen Brev `RUNNING` also records
`instance_up`.

A failure, whether the worker detects it or the instance reports
`{"step": "failed", "failed_step": "<step that failed>", "error_code": "...",
"error_detail": "...", "log_tail": "..."}`, sets `error` (with `failed_step`), records
`failed` and `teardown_requested`, and moves the session to `stopping` so the worker
deletes the instance. `stopped` is recorded once Brev no longer lists it. Logs keep the
last 200 lines or 64 KB per source; `log_tail` is stored as `failure_tail`. A retry is a
new session.

Set `SLACK_WEBHOOK_URL` to post a Slack message when the instance reports a failure
(once per session). Failures the worker detects are not posted yet, and
`deliver_stuck_alerts` (sessions with no new step for a while) is not scheduled yet.

Nothing on the instance calls `/events` or `/logs` yet; the bootstrap needs to send
them. They use the same `X-API-Key` as other routes.

## Layout

```text
backend/
  Dockerfile                 Coolify build for both API and worker (includes git and brev CLI)
  pyproject.toml, uv.lock     Python 3.12 and locked dependencies
  compose.local.yaml         Local PostgreSQL only
  .env.example               Local configuration template
  scripts/brev_spike.py      One-off measurement of real Brev behaviour (costs money)
  src/saronic_api/
    main.py                  Application factory and startup/shutdown
    config.py                Validated runtime settings
    database.py              SQLAlchemy engine, session dependency, model base
    healthcheck.py           Container probe (API or worker heartbeat); no HTTP route
    auth.py                  X-API-Key dependency
    refs.py                  Branch/tag/SHA → commit resolution (git ls-remote)
    brev/client.py           BrevClient protocol and CLI implementation
    sessions/                Models, endpoints, state machine (transitions.py), worker store,
                             timeline steps, log excerpts and Slack alerts (timeline.py)
    worker.py                Background worker entry point
  alembic.ini, migrations/   Schema migrations
  tests/                     Unit tests; PostgreSQL tests run when TEST_DATABASE_URL is set
```

## Configuration

Settings come from environment variables (and `.env` locally). Environment wins.

| Variable | Used by | Default | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | both | required | PostgreSQL URL |
| `APP_ENV` | both | `production` | `development`, `test` or `production` |
| `LOG_LEVEL` | both | `info` | |
| `SARONIC_API_KEY` | API | none | Shared key for `X-API-Key`. **The API refuses to start in production without it.** Unset in development means no auth. |
| `SARONIC_REPO_URL` | API | `https://github.com/siddharth-diwakar/saronic-f26` | Where refs are resolved |
| `SARONIC_DEFAULT_REF` | API | `main` | Ref used when the POST body has none |
| `MAX_ACTIVE_SESSIONS` | API | `1` | Cap on `requested`/`provisioning`/`running`/`stopping` sessions, to bound GPU spend |
| `BREV_API_KEY` | worker | none | **Secret.** The worker refuses to start without it. The API doesn't need it. |
| `BREV_LAUNCHABLE_ID` | worker | `env-3KBxE5OmaRkasfoXEDUhXeFleBL` | The repo's L40S Launchable |
| `BREV_CLI_PATH` | worker | `brev` | |
| `BREV_COMMAND_TIMEOUT_S` | worker | `120` | Per brev CLI call |
| `WORKER_POLL_INTERVAL_S` | worker | `10` | |
| `WORKER_HEARTBEAT_PATH` | worker | `/tmp/saronic-worker-heartbeat` | Written after every tick and Brev call |
| `PROCESS_ROLE` | healthcheck | `api` | Set to `worker` on the worker app |
| `SLACK_WEBHOOK_URL` | API | none | Optional. Slack incoming webhook for reported failures |

The Brev key is passed to the CLI only through the child process's environment,
never in argv, and is redacted from error messages.

## Local development

Install Python 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Docker with Compose. From the repository root:

```bash
cd backend
cp .env.example .env
uv sync --frozen
docker compose -f compose.local.yaml up -d --wait
uv run --frozen alembic upgrade head
uv run --frozen uvicorn saronic_api.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/docs>. Startup verifies PostgreSQL with `SELECT 1`
and fails if the database is unreachable. Start the database before the app.
Settings come from `.env` locally, with process environment variables taking
precedence. Never commit `.env`.

Try a session (add `-H "X-API-Key: ..."` if you set `SARONIC_API_KEY`):

```bash
curl -X POST localhost:8000/api/v1/sessions -H 'content-type: application/json' -d '{"ref": "main"}'
curl localhost:8000/api/v1/sessions/<id>
curl -X POST localhost:8000/api/v1/sessions/<id>/stop
```

Without a worker running, sessions stay `requested`. Stopping a `requested`
session cancels it without touching Brev.

### Running the worker locally

**A running worker with a real `BREV_API_KEY` creates real, billed L40S
instances** for any `requested` session. Install the
[brev CLI](https://github.com/brevdev/brev-cli/releases) (the image pins
v0.6.335), then in a second terminal:

```bash
BREV_API_KEY=... uv run --frozen python -m saronic_api.worker
```

It logs each create, status change and delete. Ctrl-C exits after the current
step. Because instance names are derived from session IDs, a restarted worker
picks up where it left off: it finds existing instances by name and never
creates a second one for the same session.

To exercise the full flow without Brev, point `BREV_CLI_PATH` at a fake `brev`
executable that answers `create <name> ... --detached`, `ls --json` and
`delete <name>` (see `tests/fakes.py` for the expected behaviour).

### Tests

```bash
uv run --frozen pytest
```

Tests that need PostgreSQL (row locking, `SKIP LOCKED`, the session cap) are
skipped unless `TEST_DATABASE_URL` points at a **throwaway** database. They
migrate it to head and truncate `sim_sessions` before each test:

```bash
docker compose -f compose.local.yaml exec database createdb -U saronic saronic_test
TEST_DATABASE_URL=postgresql://saronic:local-development-only@127.0.0.1:5433/saronic_test \
  uv run --frozen pytest
```

The local database uses host port **5433**, bound to localhost, to avoid a common
5432 conflict. It has a named persistent volume. To stop without deleting data:

```bash
docker compose -f compose.local.yaml down
```

Do not add `--volumes` unless you intend to erase the local database. Changing
`POSTGRES_PASSWORD` after initialization does not change existing DB credentials;
rotate them in PostgreSQL as well.

To build and run the production image locally (requires the local DB above):

```bash
docker build -t saronic-api .
docker run --rm --name saronic-api -p 127.0.0.1:8000:8000 \
  --network saronic-backend-local_default \
  -e DATABASE_URL=postgresql://saronic:local-development-only@database:5432/saronic \
  -e APP_ENV=development saronic-api
```

The same image runs the worker with
`-e PROCESS_ROLE=worker -e BREV_API_KEY=... saronic-api python -m saronic_api.worker`.

## Coolify: PostgreSQL

Use a **standalone PostgreSQL resource**, separate from the app. It gets an
independent lifecycle and backup configuration. `compose.local.yaml` is not the
production deployment definition.

1. Choose/create a Coolify project and environment (for example `saronic` /
   `production`).
2. Select **New Resource → Database → PostgreSQL**. Choose PostgreSQL **18**.
3. Select the server and destination network where the API will run. The app
   and DB must share the same server/destination for the internal URL to work.
4. Set the database name/user (for example `saronic`) and a generated strong
   password. Keep **public access disabled**. Retain persistent storage.
5. Start PostgreSQL and wait for it to become healthy.
6. Copy **Postgres URL (internal)** from its configuration. Keep the exact
   hostname and encoded credentials; don't replace them with the server IP.
7. Configure scheduled backups to your chosen supported backup destination,
   decide retention, and verify a restore before storing important data. A
   Docker volume alone is not a backup. Don't delete the database resource as
   part of app redeployments.

See [Coolify PostgreSQL](https://coolify.io/docs/databases/postgresql) and
[database networking](https://coolify.io/docs/databases).

The local setup also uses PostgreSQL 18. Its official image stores data under
`/var/lib/postgresql/18/docker`, with the volume mounted at `/var/lib/postgresql`.
Confirm the Coolify resource's storage configuration matches its PostgreSQL 18
image. See the [official image documentation](https://hub.docker.com/_/postgres).
The local volume is named `postgres18_data` so an existing PostgreSQL 17 volume
is not reused accidentally. Existing 17 data needs a deliberate dump/restore or
`pg_upgrade`; changing the image tag does not upgrade it.

## Coolify: application and auto deploy

The code must be pushed before Coolify can fetch it. Nothing is committed or
deployed by creating this folder locally.

1. In the same project/environment select **New Resource → Application** using
   a GitHub App/source connected to this repository. Choose the deployment branch
   (`main` after review, or a temporary feature branch for testing).
2. Choose the same server and destination as PostgreSQL.
3. Set the build pack/strategy to **Dockerfile**:

   | Setting | Value |
   | --- | --- |
   | Base Directory | `/backend` |
   | Dockerfile Location | `/Dockerfile` (relative to Base Directory) |
   | Ports Exposes | `8000` |
   | Build/start commands | Leave defaults; Dockerfile provides them |

   This avoids the repository root's Isaac Sim Dockerfile and GPU Compose files.
4. Add **runtime** environment variables:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | Exact internal PostgreSQL URL from the DB resource |
   | `APP_ENV` | `production` |
   | `LOG_LEVEL` | `info` |
   | `SARONIC_API_KEY` | A long random string (e.g. `openssl rand -hex 32`); share it with API clients |

   Mark the database URL and `SARONIC_API_KEY` as secrets; don't make them build
   arguments. The API refuses to start in production without `SARONIC_API_KEY`.
   The API does **not** need `BREV_API_KEY`; only the worker app gets it. Other
   settings (`MAX_ACTIVE_SESSIONS`, `SARONIC_DEFAULT_REF`, ...) are optional; see
   [Configuration](#configuration). The app accepts `postgresql://`,
   `postgres://`, or `postgresql+psycopg://` URLs.
5. Add your HTTPS domain and route it to internal port **8000**. Configure DNS
   for the Coolify server. No direct public database or API host-port mapping
   is needed; Coolify's proxy handles HTTP ingress.
6. Use the Dockerfile's **HEALTHCHECK**. It checks the local OpenAPI response and
   performs a DB query. No `/health` route exists, and `/` is intentionally 404.
   Don't configure a competing dashboard HTTP probe against `/` or `/health`.
7. Enable **Auto Deploy** and confirm the GitHub integration/webhook is active.
   With a deploy-key/public-repo source instead, configure the provider webhook
   using the URL and secret shown by Coolify. Pushes to the selected branch
   should trigger a deployment; verify this with the first approved change.
8. Deploy. Check build/runtime logs for `Database connection verified; API
   foundation ready`, a healthy container, and the session endpoints at
   `https://<your-domain>/docs`.
9. Run the database migrations (next section) before the first session request.

The container runs as a non-root user. No database migrations run automatically
at startup, and no database tables are created by the app. The DB URL is needed
at runtime only, so image builds don't require database access.

## Coolify: database migrations

Migrations are an explicit, once-per-deployment release step; neither the API
nor the worker runs them on startup. After deploying a version that adds a
migration (including the first deployment of the sessions feature, which
creates `sim_sessions` and keys the timeline tables on it), open the **API
application's terminal** in Coolify and run from `/app`:

```bash
alembic upgrade head
```

`alembic current` shows the applied revision. Run migrations before the worker
starts processing sessions; a worker started against an unmigrated database logs
errors each tick until the table exists.

Revision `e9155e165f02` drops the earlier `sessions` table and deletes any rows in
`session_events`, `session_log_excerpts` and `session_alerts`: they belonged to
`sessions` rows, which cannot be mapped to `sim_sessions`.

## Coolify: worker application

The worker is a **second Coolify application** built from the same repository
and Dockerfile. It has no HTTP server and no domain.

1. **New Resource → Application** with the same source, branch, server and
   destination as the API.
2. Build pack **Dockerfile**, Base Directory `/backend`, Dockerfile Location
   `/Dockerfile`. Leave **Ports Exposes** empty or unused and add **no domain**.
3. Override the container's start command (the image's default `CMD` starts
   the API) with:

   ```bash
   python -m saronic_api.worker
   ```

   Confirm in your Coolify version where a Dockerfile application's start
   command is overridden. If it can't be, ask before changing the shared
   Dockerfile's `CMD`.

4. Runtime environment variables: the same `DATABASE_URL`, `APP_ENV` and
   `LOG_LEVEL` as the API, plus:

   | Variable | Value |
   | --- | --- |
   | `BREV_API_KEY` | Brev API key, marked **secret** |
   | `PROCESS_ROLE` | `worker` |

   Optional: `BREV_LAUNCHABLE_ID`, `WORKER_POLL_INTERVAL_S`,
   `BREV_COMMAND_TIMEOUT_S`. The worker refuses to start without `BREV_API_KEY`.
5. Keep the Dockerfile **HEALTHCHECK**. With `PROCESS_ROLE=worker` it checks
   that the heartbeat file is fresher than 3 poll intervals plus one Brev
   command timeout (150 s by default), plus the DB. Without `PROCESS_ROLE=worker`
   the check would probe port 8000 and mark the worker unhealthy.
6. Run **exactly one** worker replica. Deploy it after the migrations above.

On redeploy the worker receives SIGTERM and exits after its current step. If it is
killed mid-step, the next worker recovers: instance names are derived from
session IDs, so it finds existing instances by name, and a retried create hits
"already exists" instead of creating a second GPU.

For separate staging/preview apps, create a separate database and runtime
configuration; do not reuse production credentials/data by default.

See [Coolify Dockerfile deployments](https://coolify.io/docs/applications/builds/dockerfile).

## Team conventions

- Session routes are registered in `create_app`. Add further domain routers
  under `src/saronic_api/` the same way. Use synchronous handlers with the
  provided synchronous DB dependency, or explicitly offload DB work from async
  handlers.
- Use `Depends(get_db)` for request-scoped sessions. Commit transactions
  explicitly; uncommitted changes roll back when the session closes.
- Define models against `database.Base` and import their modules in
  `migrations/env.py` for Alembic discovery.
- Create and review a migration when adding schema:

  ```bash
  uv run --frozen alembic revision --autogenerate -m "Describe schema change"
  uv run --frozen alembic upgrade head
  ```

  Run migrations once per deployment as an explicit release task after they
  exist, using the target environment's credentials. Never execute destructive
  schema changes automatically on every API process startup. For a running
  Coolify container, its terminal can run `alembic upgrade head` from `/app`.
- Update dependencies with `uv lock` and review both `pyproject.toml` and
  `uv.lock`. Container builds and team installs use the same frozen lockfile.
- Brev calls belong in the worker, never in API handlers. The session state
  machine is the pure `decide()` in `sessions/transitions.py`; change and test
  transitions there. Timeline events follow from status changes through
  `sync_timeline()` in `sessions/timeline.py`; call it after any new status change.

## Verification

```bash
uv run --frozen pytest   # add TEST_DATABASE_URL=... to include PostgreSQL tests
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen alembic heads
docker compose -f compose.local.yaml config --quiet
bash ../tests/test_brev_bootstrap.sh
```

Without `TEST_DATABASE_URL`, tests use a mocked database connection for
application lifecycle checks and skip the PostgreSQL tests. Tests never call
Brev. They do not prove backups, Coolify deployment, or real Brev behaviour; use
the setup steps above and the spike below for those.

## Brev spike (run once, costs money)

`scripts/brev_spike.py` measures real Brev behaviour for the repo's Launchable:
the order of `status`/`build_status` values, time to `RUNNING`, time until the
build completes, and how long deletion takes. Its results set the open
decisions on stop-vs-delete and timeouts, and what `running` should mean.

```bash
cd backend
BREV_API_KEY=... uv run --frozen python scripts/brev_spike.py --yes-this-costs-money
```

It creates `saronic-spike-<timestamp>` at the resolved commit of `--ref`
(default `main`), polls every 15 s into `spike-<timestamp>.jsonl`, and prints
each status change. Once it sees `RUNNING` + `COMPLETED` twice, it prints a
reminder to open the viewer at `http://<instance-ip>:8210` (find the IP in the
Brev console) and note when the harbor actually loads. It keeps the instance up
for `--viewer-min` (default 10) minutes; Ctrl-C ends that window early. It then
**always deletes the instance** (unless `--keep`), waits until it is gone, and
prints a summary. Other options: `--timeout-min` (default 30), `--poll-s`.

If the script is interrupted during deletion, it prints the `brev delete`
command to run. Check the Brev console afterwards either way, and post the
summary to the team.
