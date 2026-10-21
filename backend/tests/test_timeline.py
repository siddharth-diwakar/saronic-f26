import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fakes import FakeRecord
from pydantic import ValidationError

from saronic_api.config import Settings
from saronic_api.database import Base
from saronic_api.sessions.models import (
    UQ_LOG_EXCERPT,
    UQ_SESSION_ALERT,
    UQ_SESSION_EVENT,
    SimSession,
    instance_name_for,
)
from saronic_api.sessions.service import apply_claim
from saronic_api.sessions.status import INSTANCE_STEPS, SessionStatus, SessionStep
from saronic_api.sessions.timeline import (
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    InvalidSessionInput,
    InvalidSessionTransition,
    apply_reported_step,
    cap_log,
    deliver_failure_alert,
    failure_alert_text,
    post_slack,
    record_step,
    sync_timeline,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _settings(**overrides):
    return Settings(
        _env_file=None,
        database_url="postgresql://team:secret@database:5432/saronic",
        app_env="test",
        **overrides,
    )


def record(status="requested", step="requested"):
    session_id = uuid.uuid4()
    return FakeRecord(session_id, status, instance_name_for(session_id), step=step)


def at_instance_up():
    """A running session whose instance the worker has seen."""
    rec = record()
    apply_claim(rec, NOW)
    rec.status = SessionStatus.RUNNING.value
    rec.running_at = NOW
    sync_timeline(rec, NOW)
    return rec


def report(rec, step, **kwargs):
    return apply_reported_step(rec, SessionStep(step), NOW, **kwargs)


# --- Tables ---


def test_session_tables_compile_for_postgres():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    names = {"sim_sessions", "session_events", "session_log_excerpts", "session_alerts"}
    assert names <= set(Base.metadata.tables)
    assert "sessions" not in Base.metadata.tables
    for name in names:
        CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect())
    for table, constraint in [
        ("session_events", UQ_SESSION_EVENT),
        ("session_log_excerpts", UQ_LOG_EXCERPT),
        ("session_alerts", UQ_SESSION_ALERT),
    ]:
        table = Base.metadata.tables[table]
        assert constraint in {item.name for item in table.constraints}
        assert "attempt" not in table.columns
        (fk,) = table.foreign_keys
        assert fk.target_fullname == "sim_sessions.id"


# --- Steps the backend records ---


def test_sync_records_each_implied_step_once():
    rec = record()
    sync_timeline(rec, NOW)
    assert rec.steps == ["requested"]
    apply_claim(rec, NOW)
    rec.running_at = NOW
    sync_timeline(rec, NOW)
    sync_timeline(rec, NOW)
    assert rec.steps == ["requested", "provisioning", "instance_up"]
    assert rec.step == "instance_up"
    event = rec.events[-1]
    assert event.git_commit == rec.commit_sha and event.occurred_at == NOW


def test_cancel_before_provisioning_records_teardown_and_stop():
    rec = record(status="stopped")
    sync_timeline(rec, NOW)
    assert rec.steps == ["requested", "teardown_requested", "stopped"]
    assert rec.step == "stopped"


@pytest.mark.parametrize(
    ("step", "interrupted"),
    [
        ("provisioning", "instance_up"),
        ("instance_up", "container_built"),
        ("cameras_ready", "ready"),
        ("ready", "ready"),
    ],
)
def test_backend_failure_records_the_interrupted_step(step, interrupted):
    rec = record(status="stopping", step=step)
    rec.error_code = "brev_instance_failed"
    rec.error_message = "Brev reported status='FAILURE'"
    sync_timeline(rec, NOW)
    failed = next(event for event in rec.events if event.step == "failed")
    assert failed.failed_step == interrupted
    assert failed.error_code == "brev_instance_failed"
    assert failed.error_detail == "Brev reported status='FAILURE'"
    assert rec.failed_step == interrupted
    assert rec.steps[-2:] == ["failed", "teardown_requested"]
    assert rec.step == "teardown_requested"


def test_late_forward_step_never_moves_the_session_back():
    rec = record(step="container_built")
    assert record_step(rec, SessionStep.INSTANCE_UP, NOW)
    assert rec.step == "container_built"
    assert not record_step(rec, SessionStep.INSTANCE_UP, NOW)
    assert rec.steps == ["instance_up"]


# --- Steps the instance reports ---


def test_reports_advance_one_step_at_a_time_to_ready():
    rec = at_instance_up()
    for step in INSTANCE_STEPS:
        assert report(rec, step) is True
        assert rec.step == step
    assert rec.steps[-len(INSTANCE_STEPS) :] == [step.value for step in INSTANCE_STEPS]
    assert rec.status == "running"  # status stays the worker's view of Brev


def test_skipping_a_step_is_a_conflict():
    rec = at_instance_up()
    with pytest.raises(InvalidSessionTransition):
        report(rec, "scene_loaded")


def test_container_built_before_the_worker_sees_running_fills_instance_up():
    rec = record()
    apply_claim(rec, NOW)
    reported = NOW - timedelta(seconds=3)
    assert report(rec, "container_built", occurred_at=reported)
    assert rec.steps == ["requested", "provisioning", "instance_up", "container_built"]
    assert rec.events[-1].occurred_at == reported
    # The worker's later RUNNING transition does not record instance_up again.
    rec.status, rec.running_at = "running", NOW
    sync_timeline(rec, NOW)
    assert rec.steps.count("instance_up") == 1
    assert rec.step == "container_built"


def test_repeated_report_is_a_noop_even_after_stop():
    rec = at_instance_up()
    report(rec, "container_built")
    assert report(rec, "container_built") is False
    rec.status = "stopped"
    assert report(rec, "container_built") is False
    assert rec.steps.count("container_built") == 1


@pytest.mark.parametrize(
    "step", ["requested", "provisioning", "instance_up", "teardown_requested", "stopped"]
)
def test_backend_steps_cannot_be_reported(step):
    with pytest.raises(InvalidSessionInput):
        report(at_instance_up(), step)


def test_error_fields_are_rejected_on_a_normal_step():
    with pytest.raises(InvalidSessionInput):
        report(at_instance_up(), "container_built", error_code="NOPE")


@pytest.mark.parametrize("status", ["requested", "stopping", "stopped", "failed"])
def test_reports_are_rejected_unless_the_instance_may_be_starting(status):
    rec = at_instance_up()
    rec.status = status
    with pytest.raises(InvalidSessionTransition):
        report(rec, "container_built")


def test_reported_failure_records_the_error_and_stops_the_session():
    rec = at_instance_up()
    report(rec, "container_built")
    assert report(
        rec,
        "failed",
        failed_step=SessionStep.SCENE_LOADED,
        error_code=" ISAAC_LAUNCH_FAILED ",
        error_detail=" kit exited 1 ",
    )
    assert rec.status == "stopping"  # the worker deletes the instance next
    assert rec.error_code == "ISAAC_LAUNCH_FAILED"
    assert rec.error_message == "kit exited 1"
    assert rec.failed_step == "scene_loaded"
    assert rec.steps[-2:] == ["failed", "teardown_requested"]
    failed = rec.events[-2]
    assert (failed.failed_step, failed.error_code) == ("scene_loaded", "ISAAC_LAUNCH_FAILED")


def test_failure_after_teardown_is_not_recorded_twice():
    """Regression: a second failure used to hit the unique constraint and return 500."""
    rec = at_instance_up()
    rec.status = "stopping"
    rec.error_code, rec.error_message = "brev_instance_failed", "UNHEALTHY"
    sync_timeline(rec, NOW)
    assert report(rec, "failed", failed_step=SessionStep.CONTAINER_BUILT, error_code="X") is False
    assert rec.steps.count("failed") == 1
    assert rec.error_code == "brev_instance_failed"
    with pytest.raises(InvalidSessionTransition):
        report(rec, "container_built")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"error_code": "MISSING_STEP"},
        {"failed_step": SessionStep.FAILED, "error_code": "NOT_A_PIPELINE_STEP"},
        {"failed_step": SessionStep.CONTAINER_BUILT},
        {"failed_step": SessionStep.CONTAINER_BUILT, "error_code": "   "},
        {"failed_step": SessionStep.CONTAINER_BUILT, "error_code": "X" * 65},
    ],
)
def test_failure_reports_are_validated(kwargs):
    rec = at_instance_up()
    with pytest.raises(InvalidSessionInput):
        report(rec, "failed", **kwargs)
    assert rec.status == "running" and "failed" not in rec.steps


# --- Logs and alerts ---


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
    row = SimSession(
        id=uuid.uuid4(),
        status=SessionStatus.STOPPING.value,
        step=SessionStep.TEARDOWN_REQUESTED.value,
        commit_sha="abc123" + "0" * 34,
        instance_name="saronic-abc",
        instance_id="inst-1",
        failed_step=SessionStep.CONTAINER_BUILT.value,
        error_code="DOCKER_BUILD_FAILED",
        error_message="build exited 1",
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

    monkeypatch.setattr("saronic_api.sessions.timeline.urllib.request.urlopen", fake_urlopen)
    post_slack("https://hooks.slack.example/abc", "session failed")
    assert captured["url"] == "https://hooks.slack.example/abc"
    assert b"session failed" in captured["body"]
    assert captured["timeout"] == 5


def test_blank_slack_webhook_is_disabled():
    config = _settings(slack_webhook_url="  ")
    assert config.slack_webhook_url is None
    with pytest.raises(ValidationError):
        _settings(slack_webhook_url="not-a-url")
