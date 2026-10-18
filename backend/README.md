# Backend infrastructure

CPU-only FastAPI foundation, deployed independently of Isaac Sim and Brev.
Session timeline routes and tables are in place. There are no Brev credentials,
provisioning workers, or GPU dependencies yet. FastAPI's built-in `/docs`,
`/redoc`, and `/openapi.json` are available; `/` returns 404. Authentication
is not implemented.

## Layout

```text
backend/
  Dockerfile                 Coolify application build
  pyproject.toml, uv.lock     Python 3.12 and locked dependencies
  compose.local.yaml         Local PostgreSQL only
  .env.example               Local configuration template
  src/saronic_api/
    main.py                  Application factory and startup/shutdown
    config.py                Validated runtime settings
    database.py              SQLAlchemy engine, session dependency, model base
    models.py                Session timeline tables
    timeline.py              Step changes, log excerpts, Slack alerts
    schemas.py               Session request and response bodies
    routes.py                Session HTTP routes
    healthcheck.py           Container probe; no custom HTTP route
  alembic.ini, migrations/   Schema migrations, including the session timeline
  tests/                     Timeline rules and startup checks without a database
```

## Local development

Install Python 3.12, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and Docker with Compose. From the repository root:

```bash
cd backend
cp .env.example .env
uv sync --frozen
docker compose -f compose.local.yaml up -d --wait
uv run --frozen uvicorn saronic_api.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/docs>. The empty API is intentional. Startup verifies
PostgreSQL with `SELECT 1` and fails if the database is unreachable. Start the
database before the app. Settings come from `.env` locally, with process
environment variables taking precedence. Never commit `.env`.

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

   Mark the database URL as a secret; don't make it a build argument. The app
   accepts `postgresql://`, `postgres://`, or `postgresql+psycopg://` URLs.
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
   foundation ready` and a healthy container. Apply migrations once per
   environment (`alembic upgrade head` from `/app`) before expecting session
   data. Confirm `https://<your-domain>/docs` lists the session routes.

The container runs as a non-root user. No database migrations run automatically
at startup, and no database tables are created by the app. The DB URL is needed
at runtime only, so image builds don't require database access.

For separate staging/preview apps, create a separate database and runtime
configuration; do not reuse production credentials/data by default.

See [Coolify Dockerfile deployments](https://coolify.io/docs/applications/builds/dockerfile).

## Session timeline

Apply the revision once per environment. Startup does not run migrations.

```bash
cd backend
uv run --frozen alembic upgrade head
```

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sessions` | Open a session at `requested` |
| `GET` | `/sessions/{id}` | Status, full event timeline, current attempt's log excerpts |
| `POST` | `/sessions/{id}/events` | Next step, failure, teardown, or stop |
| `POST` | `/sessions/{id}/logs` | Capped startup log (`bootstrap`, `docker_build`, `isaac_launch`) |
| `POST` | `/sessions/{id}/attempts` | Retry after `failed` or `stopped`; prior events stay |

Each step change is one transaction: the session row and the new event commit
together. Stopping a live session appends `teardown_requested`, then `stopped`.
A retry increments `attempt` and appends a new `requested` event.

Failure stores `failed_step`, `error_code`, `error_detail`, and an optional
`log_tail` (last 200 lines or 64 KB). Set `SLACK_WEBHOOK_URL` to alert on
failure. `deliver_stuck_alerts` can be called by a future worker for sessions
that stay in progress; nothing schedules it yet.

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
- Future durable provisioning jobs belong in a separate worker; the skeleton
  intentionally does not create one before the job contract is decided.

## Verification

```bash
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen alembic heads
docker compose -f compose.local.yaml config --quiet
```

Tests use a mocked database connection for application lifecycle checks. They
do not prove PostgreSQL networking, backups, Docker builds, or Coolify deployment;
verify those on the target host using the setup steps above.
