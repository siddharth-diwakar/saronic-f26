"""Session API tests.

Tests marked requires_db need PostgreSQL (SKIP LOCKED / FOR UPDATE / advisory locks), so
SQLite is not an option. Point TEST_DATABASE_URL at a throwaway database; it is migrated
to head and sim_sessions is truncated between tests. See backend/README.md.
"""

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.orm import Session

from saronic_api.config import Settings
from saronic_api.database import database_url
from saronic_api.main import create_app
from saronic_api.refs import (
    AmbiguousRef,
    InvalidRef,
    RefNotFound,
    RefResolutionError,
    ResolvedRef,
)
from saronic_api.sessions.models import SimSession
from saronic_api.sessions.router import get_ref_resolver

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to a throwaway PostgreSQL database"
)

API_KEY = "session-api-test-key"
BREV_KEY = "brev-key-must-never-leak"
SHA = "a" * 40
AUTH = {"X-API-Key": API_KEY}


def fake_resolve(ref):
    errors = {
        "bad ref": InvalidRef("ref must contain no whitespace"),
        "shared": AmbiguousRef("ref 'shared' is both a branch and a tag"),
        "missing": RefNotFound("ref 'missing' was not found"),
        "offline": RefResolutionError("could not list refs from the repository"),
    }
    if ref in errors:
        raise errors[ref]
    return ResolvedRef(ref, SHA)


def make_settings(**overrides):
    values = {
        "database_url": TEST_DATABASE_URL or "postgresql://team:secret@database:5432/saronic",
        "app_env": "test",
        "SARONIC_API_KEY": API_KEY,
        "brev_api_key": BREV_KEY,
    }
    return Settings(_env_file=None, **{**values, **overrides})


@pytest.fixture
def no_subprocesses(monkeypatch):
    """The API must never shell out to brev (or git, which the fake resolver replaces)."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"API ran a subprocess: {args}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("saronic_api.brev.client.CliBrevClient._run", forbidden)


def client_for(settings, monkeypatch=None, *, mock_db=False):
    if mock_db:
        monkeypatch.setattr("saronic_api.main.build_engine", lambda _: Mock())
        monkeypatch.setattr("saronic_api.main.check_database", Mock())
    app = create_app(settings)
    app.dependency_overrides[get_ref_resolver] = lambda: fake_resolve
    return TestClient(app)


# --- No database needed ---


def test_production_without_api_key_fails_startup(monkeypatch):
    build = Mock()
    monkeypatch.setattr("saronic_api.main.build_engine", build)
    settings = make_settings(app_env="production", SARONIC_API_KEY=None)
    with pytest.raises(RuntimeError, match="SARONIC_API_KEY"), TestClient(create_app(settings)):
        pass
    build.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_missing_or_wrong_key_is_401(monkeypatch, headers):
    with client_for(make_settings(), monkeypatch, mock_db=True) as client:
        sid = uuid.uuid4()
        for method, path in [
            ("post", "/api/v1/sessions"),
            ("get", f"/api/v1/sessions/{sid}"),
            ("post", f"/api/v1/sessions/{sid}/stop"),
        ]:
            response = client.request(method, path, headers=headers)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "ApiKey"


def test_no_key_configured_outside_production_allows_requests(monkeypatch):
    settings = make_settings(app_env="development", SARONIC_API_KEY=None)
    with client_for(settings, monkeypatch, mock_db=True) as client:
        # Passes auth and fails on the ref, before any DB access.
        response = client.post("/api/v1/sessions", json={"ref": "bad ref"})
        assert response.status_code == 422


@pytest.mark.parametrize(
    ("ref", "code"), [("bad ref", 422), ("shared", 422), ("missing", 422), ("offline", 502)]
)
def test_ref_errors(monkeypatch, no_subprocesses, ref, code):
    with client_for(make_settings(), monkeypatch, mock_db=True) as client:
        response = client.post("/api/v1/sessions", json={"ref": ref}, headers=AUTH)
        assert response.status_code == code
        assert isinstance(response.json()["detail"], str)


def test_unknown_body_fields_rejected(monkeypatch):
    with client_for(make_settings(), monkeypatch, mock_db=True) as client:
        response = client.post("/api/v1/sessions", json={"branch": "main"}, headers=AUTH)
        assert response.status_code == 422


# --- PostgreSQL ---


@pytest.fixture(scope="module")
def migrated_db():
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
    engine = create_engine(database_url(make_settings()))
    yield engine
    engine.dispose()


@pytest.fixture
def db_engine(migrated_db):
    with migrated_db.begin() as connection:
        connection.execute(text("TRUNCATE sim_sessions"))
    return migrated_db


@pytest.fixture
def api(db_engine, no_subprocesses):
    with client_for(make_settings()) as client:
        yield client


def set_status(engine, session_id, status):
    with Session(engine) as db:
        db.execute(update(SimSession).where(SimSession.id == session_id).values(status=status))
        db.commit()


def create(api, **body):
    response = api.post("/api/v1/sessions", json=body or None, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()


@requires_db
def test_create_returns_202_with_resolved_commit_and_derived_instance(api):
    body = create(api)
    sid = uuid.UUID(body["id"])
    assert body["status"] == "requested"
    assert body["ref"] == "main"  # SARONIC_DEFAULT_REF
    assert body["commit_sha"] == SHA
    assert body["brev"] == {
        "instance_name": f"saronic-{sid.hex[:12]}",
        "instance_id": None,
        "status": None,
        "build_status": None,
        "health_status": None,
        "checked_at": None,
    }
    assert body["error"] is None
    assert body["created_at"] and body["updated_at"]
    assert body["running_at"] is None and body["ended_at"] is None


@requires_db
def test_create_with_explicit_ref_and_get(api):
    body = create(api, ref="team/feature")
    got = api.get(f"/api/v1/sessions/{body['id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json() == body
    assert got.json()["ref"] == "team/feature"
    assert BREV_KEY not in got.text and API_KEY not in got.text


@requires_db
@pytest.mark.parametrize("ref", ["bad ref", "offline"])
def test_ref_errors_insert_nothing(api, db_engine, ref):
    assert api.post("/api/v1/sessions", json={"ref": ref}, headers=AUTH).status_code in (422, 502)
    with Session(db_engine) as db:
        assert db.query(SimSession).count() == 0


@requires_db
def test_cap_returns_409_until_a_session_ends(api, db_engine):
    first = create(api)
    response = api.post("/api/v1/sessions", headers=AUTH)
    assert response.status_code == 409
    for status in ["provisioning", "running", "stopping"]:
        set_status(db_engine, first["id"], status)
        assert api.post("/api/v1/sessions", headers=AUTH).status_code == 409, status
    set_status(db_engine, first["id"], "stopped")
    create(api)


@requires_db
def test_concurrent_creates_respect_cap(db_engine, no_subprocesses):
    with (
        client_for(make_settings(max_active_sessions=2)) as api,
        ThreadPoolExecutor(max_workers=6) as pool,
    ):
        codes = list(
            pool.map(lambda _: api.post("/api/v1/sessions", headers=AUTH).status_code, range(6))
        )
    assert sorted(codes) == [202, 202, 409, 409, 409, 409]


@requires_db
def test_get_unknown_is_404_and_bad_id_is_422(api):
    assert api.get(f"/api/v1/sessions/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert api.get("/api/v1/sessions/not-a-uuid", headers=AUTH).status_code == 422
    assert api.post(f"/api/v1/sessions/{uuid.uuid4()}/stop", headers=AUTH).status_code == 404


@requires_db
def test_stop_requested_cancels_and_is_idempotent(api):
    sid = create(api)["id"]
    first = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH)
    assert first.status_code == 202
    assert first.json()["status"] == "stopped"
    assert first.json()["ended_at"] is not None
    assert first.json()["stop_requested_at"] is None
    again = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH)
    assert again.status_code == 202
    assert again.json()["status"] == "stopped"
    assert again.json()["ended_at"] == first.json()["ended_at"]


@requires_db
@pytest.mark.parametrize("status", ["provisioning", "running"])
def test_stop_active_goes_to_stopping_and_is_idempotent(api, db_engine, status):
    sid = create(api)["id"]
    set_status(db_engine, sid, status)
    first = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH).json()
    assert first["status"] == "stopping"
    assert first["stop_requested_at"] is not None and first["ended_at"] is None
    again = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH).json()
    assert again["status"] == "stopping"
    assert again["stop_requested_at"] == first["stop_requested_at"]


@requires_db
@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_stop_terminal_changes_nothing(api, db_engine, status):
    sid = create(api)["id"]
    set_status(db_engine, sid, status)
    body = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH).json()
    assert body["status"] == status
    assert body["stop_requested_at"] is None
