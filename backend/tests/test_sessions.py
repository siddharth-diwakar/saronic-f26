import itertools
import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from saronic_api.config import Settings
from saronic_api.database import Base, get_db
from saronic_api.main import create_app
from saronic_api.models import (
    UQ_LOG_EXCERPT,
    UQ_SESSION_ALERT,
    UQ_SESSION_EVENT,
    LogSource,
    SessionStep,
    SimulationSession,
)
from saronic_api.timeline import (
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    InvalidSessionInput,
    InvalidSessionTransition,
    cap_log,
    deliver_failure_alert,
    failure_alert_text,
    plan_retry,
    plan_transition,
    post_slack,
)

PIPELINE = (
    SessionStep.REQUESTED,
    SessionStep.PROVISIONING,
    SessionStep.INSTANCE_UP,
    SessionStep.CONTAINER_BUILT,
    SessionStep.SCENE_LOADED,
    SessionStep.PHYSICS_RUNNING,
    SessionStep.CAMERAS_READY,
    SessionStep.READY,
)


def _settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql://team:secret@database:5432/saronic",
        **overrides,
    )


def test_session_tables_compile_for_postgres():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    dialect = postgresql.dialect()
    names = {table.name for table in Base.metadata.sorted_tables}
    assert {
        "sessions",
        "session_events",
        "session_log_excerpts",
        "session_alerts",
    } <= names
    for table in Base.metadata.sorted_tables:
        if table.name in names:
            CreateTable(table).compile(dialect=dialect)
    assert UQ_SESSION_EVENT in {
        item.name for item in Base.metadata.tables["session_events"].constraints
    }
    assert UQ_LOG_EXCERPT in {
        item.name for item in Base.metadata.tables["session_log_excerpts"].constraints
    }
    assert UQ_SESSION_ALERT in {
        item.name for item in Base.metadata.tables["session_alerts"].constraints
    }


def test_forward_pipeline_allows_only_the_next_step():
    for current, nxt in itertools.pairwise(PIPELINE):
        plan = plan_transition(current, nxt, failed_step=None, error_code=None, error_detail=None)
        assert plan.noop is False
        assert plan.status is nxt
    with pytest.raises(InvalidSessionTransition):
        plan_transition(
            SessionStep.REQUESTED,
            SessionStep.INSTANCE_UP,
            failed_step=None,
            error_code=None,
            error_detail=None,
        )


def test_repeating_the_current_step_is_a_noop():
    plan = plan_transition(
        SessionStep.PROVISIONING,
        SessionStep.PROVISIONING,
        failed_step=None,
        error_code=None,
        error_detail=None,
    )
    assert plan.noop is True
    assert plan.status is SessionStep.PROVISIONING


def test_failure_records_the_step_and_error_and_blocks_success_terminal():
    plan = plan_transition(
        SessionStep.INSTANCE_UP,
        SessionStep.FAILED,
        failed_step=SessionStep.CONTAINER_BUILT,
        error_code=" DOCKER_BUILD_FAILED ",
        error_detail=" build exited 1 ",
    )
    assert plan.noop is False
    assert plan.failed_step is SessionStep.CONTAINER_BUILT
    assert plan.error_code == "DOCKER_BUILD_FAILED"
    assert plan.error_detail == "build exited 1"
    with pytest.raises(InvalidSessionTransition):
        plan_transition(
            SessionStep.READY,
            SessionStep.FAILED,
            failed_step=SessionStep.READY,
            error_code="LATE",
            error_detail=None,
        )
    with pytest.raises(InvalidSessionInput):
        plan_transition(
            SessionStep.PROVISIONING,
            SessionStep.FAILED,
            failed_step=None,
            error_code="MISSING_STEP",
            error_detail=None,
        )


def test_error_fields_are_rejected_on_a_normal_step():
    with pytest.raises(InvalidSessionInput):
        plan_transition(
            SessionStep.REQUESTED,
            SessionStep.PROVISIONING,
            failed_step=None,
            error_code="NOPE",
            error_detail=None,
        )


def test_teardown_and_stop_are_compensating_events():
    ready = plan_transition(
        SessionStep.READY,
        SessionStep.TEARDOWN_REQUESTED,
        failed_step=None,
        error_code=None,
        error_detail=None,
    )
    assert ready.status is SessionStep.TEARDOWN_REQUESTED
    stopped = plan_transition(
        SessionStep.TEARDOWN_REQUESTED,
        SessionStep.STOPPED,
        failed_step=None,
        error_code=None,
        error_detail=None,
    )
    assert stopped.status is SessionStep.STOPPED
    failed_stop = plan_transition(
        SessionStep.FAILED,
        SessionStep.STOPPED,
        failed_step=None,
        error_code=None,
        error_detail=None,
    )
    assert failed_stop.status is SessionStep.STOPPED
    with pytest.raises(InvalidSessionTransition):
        plan_transition(
            SessionStep.READY,
            SessionStep.STOPPED,
            failed_step=None,
            error_code=None,
            error_detail=None,
        )


def test_retry_is_only_allowed_after_failure_or_stop():
    plan_retry(SessionStep.FAILED)
    plan_retry(SessionStep.STOPPED)
    with pytest.raises(InvalidSessionTransition):
        plan_retry(SessionStep.PROVISIONING)


def test_log_cap_keeps_the_tail():
    short, count, truncated = cap_log("one\r\ntwo")
    assert short == "one\ntwo"
    assert count == 2
    assert truncated is False

    body = "\n".join(f"line-{index}" for index in range(MAX_LOG_LINES + 5))
    capped, count, truncated = cap_log(body)
    assert truncated is True
    assert count == MAX_LOG_LINES
    assert capped.startswith(f"line-{5}")
    assert capped.endswith(f"line-{MAX_LOG_LINES + 4}")

    huge = "x" * (MAX_LOG_BYTES + 50)
    capped, _, truncated = cap_log(huge)
    assert truncated is True
    assert len(capped.encode()) <= MAX_LOG_BYTES

    assert cap_log("") == ("", 0, False)


def test_failure_alert_omits_log_bodies_and_skips_without_a_webhook():
    row = SimulationSession(
        id=uuid.uuid4(),
        status=SessionStep.FAILED.value,
        attempt=1,
        git_commit="abc123",
        brev_instance_id="inst-1",
        failed_step=SessionStep.CONTAINER_BUILT.value,
        error_code="DOCKER_BUILD_FAILED",
        error_detail="build exited 1",
    )
    text = failure_alert_text(row)
    assert "abc123" in text
    assert "inst-1" in text
    assert "container_built" in text
    assert "DOCKER_BUILD_FAILED" in text
    assert "build exited 1" in text
    db = Mock()
    deliver_failure_alert(db, _settings(), row)
    db.execute.assert_not_called()


def test_slack_post_sends_text_only(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("saronic_api.timeline.urllib.request.urlopen", fake_urlopen)
    post_slack("https://hooks.slack.example/abc", "session failed")
    assert captured["url"] == "https://hooks.slack.example/abc"
    assert b"session failed" in captured["body"]
    assert captured["timeout"] == 5


def test_blank_slack_webhook_is_disabled():
    config = _settings(slack_webhook_url="  ")
    assert config.slack_webhook_url is None
    with pytest.raises(ValidationError):
        _settings(slack_webhook_url="not-a-url")


def test_session_routes_map_conflicts_and_missing_sessions(monkeypatch):
    engine = Mock()
    monkeypatch.setattr("saronic_api.main.build_engine", lambda _: engine)
    monkeypatch.setattr("saronic_api.main.check_database", Mock())
    app = create_app(_settings())

    def override_db():
        yield Mock()

    app.dependency_overrides[get_db] = override_db
    session_id = uuid.uuid4()

    def missing(_db, _session_id):
        return None

    monkeypatch.setattr("saronic_api.routes.load_session", missing)
    with TestClient(app) as client:
        assert client.get(f"/sessions/{session_id}").status_code == 404
        monkeypatch.setattr(
            "saronic_api.routes.apply_step",
            Mock(side_effect=InvalidSessionTransition(SessionStep.READY, SessionStep.FAILED)),
        )
        conflict = client.post(
            f"/sessions/{session_id}/events",
            json={"step": "failed", "failed_step": "ready", "error_code": "LATE"},
        )
        assert conflict.status_code == 409
        created = datetime.now(UTC)
        row = SimulationSession(
            id=session_id,
            status=SessionStep.REQUESTED.value,
            attempt=1,
            git_commit="abc123",
            created_at=created,
            updated_at=created,
        )
        row.events = []
        row.log_excerpts = []
        monkeypatch.setattr("saronic_api.routes.open_session", lambda *args, **kwargs: row)
        monkeypatch.setattr("saronic_api.routes.load_session", lambda *args, **kwargs: row)
        response = client.post("/sessions", json={"git_commit": "abc123"})
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "requested"
        assert body["timeline"] == []
        assert body["logs"] == {}
        assert LogSource.BOOTSTRAP.value == "bootstrap"
    engine.dispose.assert_called_once()
