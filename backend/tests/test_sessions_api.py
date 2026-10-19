"""Session API tests. Tests using `db_engine`/`api` need TEST_DATABASE_URL (see conftest.py)."""

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from saronic_api.config import Settings
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
from saronic_api.sessions.timeline import find_stuck_sessions

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

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
            ("post", f"/api/v1/sessions/{sid}/events"),
            ("post", f"/api/v1/sessions/{sid}/logs"),
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


@pytest.fixture
def api(db_engine, no_subprocesses):
    with client_for(make_settings()) as client:
        yield client


def set_status(engine, session_id, status, **fields):
    with Session(engine) as db:
        db.execute(
            update(SimSession).where(SimSession.id == session_id).values(status=status, **fields)
        )
        db.commit()


def create(api, **body):
    response = api.post("/api/v1/sessions", json=body or None, headers=AUTH)
    assert response.status_code == 202, response.text
    return response.json()


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
    assert body["step"] == "requested"
    assert [event["step"] for event in body["timeline"]] == ["requested"]
    assert body["timeline"][0]["git_commit"] == SHA
    assert body["logs"] == {}


def test_create_with_explicit_ref_and_get(api):
    body = create(api, ref="team/feature")
    got = api.get(f"/api/v1/sessions/{body['id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json() == body
    assert got.json()["ref"] == "team/feature"
    assert BREV_KEY not in got.text and API_KEY not in got.text


@pytest.mark.parametrize("ref", ["bad ref", "offline"])
def test_ref_errors_insert_nothing(api, db_engine, ref):
    assert api.post("/api/v1/sessions", json={"ref": ref}, headers=AUTH).status_code in (422, 502)
    with Session(db_engine) as db:
        assert db.query(SimSession).count() == 0


def test_cap_returns_409_until_a_session_ends(api, db_engine):
    first = create(api)
    response = api.post("/api/v1/sessions", headers=AUTH)
    assert response.status_code == 409
    for status in ["provisioning", "running", "stopping"]:
        set_status(db_engine, first["id"], status)
        assert api.post("/api/v1/sessions", headers=AUTH).status_code == 409, status
    set_status(db_engine, first["id"], "stopped")
    create(api)


def test_concurrent_creates_respect_cap(db_engine, no_subprocesses):
    with (
        client_for(make_settings(max_active_sessions=2)) as api,
        ThreadPoolExecutor(max_workers=6) as pool,
    ):
        codes = list(
            pool.map(lambda _: api.post("/api/v1/sessions", headers=AUTH).status_code, range(6))
        )
    assert sorted(codes) == [202, 202, 409, 409, 409, 409]


def test_get_unknown_is_404_and_bad_id_is_422(api):
    assert api.get(f"/api/v1/sessions/{uuid.uuid4()}", headers=AUTH).status_code == 404
    assert api.get("/api/v1/sessions/not-a-uuid", headers=AUTH).status_code == 422
    assert api.post(f"/api/v1/sessions/{uuid.uuid4()}/stop", headers=AUTH).status_code == 404


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
    assert steps(again.json()) == ["requested", "teardown_requested", "stopped"]


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


@pytest.mark.parametrize("status", ["stopped", "failed"])
def test_stop_terminal_changes_nothing(api, db_engine, status):
    sid = create(api)["id"]
    set_status(db_engine, sid, status)
    body = api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH).json()
    assert body["status"] == status
    assert body["stop_requested_at"] is None


# --- Timeline: instance-reported steps and logs ---


def steps(body):
    return [event["step"] for event in body["timeline"]]


def event(api, sid, **body):
    return api.post(f"/api/v1/sessions/{sid}/events", json=body, headers=AUTH)


def running(api, db_engine):
    """A session the worker has seen come up on Brev."""
    sid = create(api)["id"]
    set_status(db_engine, sid, "running", step="instance_up")
    return sid


def test_reported_steps_reach_ready_and_repeats_are_noops(api, db_engine):
    sid = running(api, db_engine)
    for step in ["container_built", "scene_loaded", "physics_running", "cameras_ready", "ready"]:
        response = event(api, sid, step=step)
        assert response.status_code == 200, response.text
        assert response.json()["step"] == step
    again = event(api, sid, step="ready")
    assert again.status_code == 200
    body = api.get(f"/api/v1/sessions/{sid}", headers=AUTH).json()
    assert body["status"] == "running" and body["step"] == "ready"
    assert steps(body)[-5:] == [
        "container_built",
        "scene_loaded",
        "physics_running",
        "cameras_ready",
        "ready",
    ]
    assert steps(body).count("ready") == 1


def test_report_before_worker_sees_running_fills_instance_up(api, db_engine):
    sid = create(api)["id"]
    set_status(db_engine, sid, "provisioning", step="provisioning")
    body = event(api, sid, step="container_built").json()
    assert steps(body)[-2:] == ["instance_up", "container_built"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"step": "scene_loaded"}, 409),  # skips container_built
        ({"step": "instance_up"}, 422),  # recorded by the backend
        ({"step": "container_built", "error_code": "NOPE"}, 422),
        ({"step": "container_built", "log_tail": "x"}, 422),
        ({"step": "failed", "error_code": "NO_STEP"}, 422),
        ({"step": "container_built", "brev_instance_id": "x"}, 422),  # unknown field
    ],
)
def test_invalid_reports_are_rejected_and_change_nothing(api, db_engine, body, code):
    sid = running(api, db_engine)
    response = event(api, sid, **body)
    assert response.status_code == code, response.text
    assert isinstance(response.json()["detail"], (str, list))
    after = api.get(f"/api/v1/sessions/{sid}", headers=AUTH).json()
    assert after["step"] == "instance_up" and after["status"] == "running"


def test_reports_for_unknown_or_ended_sessions(api, db_engine):
    assert event(api, uuid.uuid4(), step="container_built").status_code == 404
    sid = create(api)["id"]
    assert event(api, sid, step="container_built").status_code == 409  # not provisioned yet
    api.post(f"/api/v1/sessions/{sid}/stop", headers=AUTH)
    assert event(api, sid, step="container_built").status_code == 409


def test_reported_failure_stops_the_session_and_alerts_once(monkeypatch, db_engine):
    posted = []
    monkeypatch.setattr(
        "saronic_api.sessions.timeline.post_slack", lambda url, text: posted.append(text)
    )
    settings = make_settings(slack_webhook_url="https://hooks.slack.example/T/B/x")
    with client_for(settings) as api:
        sid = running(api, db_engine)
        event(api, sid, step="container_built")
        failure = {
            "step": "failed",
            "failed_step": "scene_loaded",
            "error_code": "ISAAC_LAUNCH_FAILED",
            "error_detail": "kit exited 1",
            "log_tail": "\n".join(f"line {i}" for i in range(250)),
        }
        response = event(api, sid, **failure)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "stopping"  # the worker deletes the instance next
        assert body["error"] == {
            "code": "ISAAC_LAUNCH_FAILED",
            "message": "kit exited 1",
            "failed_step": "scene_loaded",
        }
        assert steps(body)[-2:] == ["failed", "teardown_requested"]
        tail = body["logs"]["failure_tail"].split("\n")
        assert len(tail) == 200 and tail[-1] == "line 249"

        # Regression: a repeated failure used to return 500 (unique violation).
        assert event(api, sid, **failure).status_code == 200
        assert event(api, sid, step="scene_loaded").status_code == 409
    assert len(posted) == 1
    assert "ISAAC_LAUNCH_FAILED" in posted[0] and "scene_loaded" in posted[0]


def test_logs_are_capped_and_replaced_per_source(api):
    sid = create(api)["id"]
    url = f"/api/v1/sessions/{sid}/logs"
    long = "\n".join(f"build {i}" for i in range(300))
    first = api.post(url, json={"source": "docker_build", "body": long}, headers=AUTH)
    assert first.status_code == 200, first.text
    assert len(first.json()["logs"]["docker_build"].split("\n")) == 200
    api.post(url, json={"source": "docker_build", "body": "rebuilt"}, headers=AUTH)
    api.post(url, json={"source": "bootstrap", "body": "cloned"}, headers=AUTH)
    logs = api.get(f"/api/v1/sessions/{sid}", headers=AUTH).json()["logs"]
    assert logs == {"docker_build": "rebuilt", "bootstrap": "cloned"}
    assert api.post(url, json={"source": "nope", "body": "x"}, headers=AUTH).status_code == 422
    missing = f"/api/v1/sessions/{uuid.uuid4()}/logs"
    assert (
        api.post(missing, json={"source": "bootstrap", "body": "x"}, headers=AUTH).status_code
        == 404
    )


def test_stuck_sessions_are_those_without_a_recent_step(api, db_engine):
    sid = running(api, db_engine)
    later = datetime.now(UTC) + timedelta(minutes=1)
    earlier = datetime.now(UTC) - timedelta(minutes=1)
    with Session(db_engine) as db:
        assert [s.id for s in find_stuck_sessions(db, older_than=later)] == [uuid.UUID(sid)]
        assert find_stuck_sessions(db, older_than=earlier) == []
    for step in ["container_built", "scene_loaded", "physics_running", "cameras_ready", "ready"]:
        event(api, sid, step=step)
    with Session(db_engine) as db:
        assert find_stuck_sessions(db, older_than=later) == []
