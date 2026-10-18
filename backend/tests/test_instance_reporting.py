import uuid
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from saronic_api.config import Settings
from saronic_api.database import get_db
from saronic_api.main import create_app
from saronic_api.models import SessionStep
from saronic_api.schemas import SessionOut
from saronic_api.timeline import StepResult

TOKEN = "instance-token"
EVENT = {"step": "failed", "failed_step": "container_built", "error_code": "BUILD"}


@pytest.fixture
def make_client(monkeypatch):
    engine = Mock()
    monkeypatch.setattr("saronic_api.main.build_engine", lambda _: engine)
    monkeypatch.setattr("saronic_api.main.check_database", Mock())
    now = datetime.now(UTC)
    detail = SessionOut(
        id=uuid.uuid4(),
        status=SessionStep.FAILED,
        attempt=1,
        brev_instance_id=None,
        git_commit="abc123",
        failed_step=SessionStep.CONTAINER_BUILT,
        error_code="BUILD",
        error_detail=None,
        created_at=now,
        updated_at=now,
        timeline=[],
        logs={},
    )
    monkeypatch.setattr("saronic_api.routes._detail", lambda db, session_id: detail)
    monkeypatch.setattr("saronic_api.routes.record_log", Mock())
    alert = Mock()
    monkeypatch.setattr("saronic_api.routes._send_failure_alert", alert)

    def make(changed=True, **overrides):
        result = StepResult(session_id=uuid.uuid4(), changed=changed, status=SessionStep.FAILED)
        monkeypatch.setattr("saronic_api.routes.apply_step", Mock(return_value=result))
        values = {"database_url": "postgresql://team:secret@database:5432/saronic"}
        app = create_app(Settings(_env_file=None, **{**values, **overrides}))

        def override_db():
            yield Mock()

        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)
        client.alert, client.engine = alert, engine
        return client

    return make


def post(client, path, body, token=None):
    headers = {"X-Ingest-Token": token} if token else {}
    return client.post(f"/sessions/{uuid.uuid4()}/{path}", json=body, headers=headers)


@pytest.mark.parametrize(
    ("path", "body"), [("events", EVENT), ("logs", {"source": "bootstrap", "body": "x"})]
)
def test_instance_routes_require_the_token(make_client, path, body):
    with make_client(ingest_token=TOKEN) as client:
        assert post(client, path, body).status_code == 401
        assert post(client, path, body, "wrong").status_code == 401
        assert post(client, path, body, TOKEN).status_code == 200
    with make_client() as client:  # Production without a token stays closed.
        assert post(client, path, body, TOKEN).status_code == 503
    with make_client(app_env="development") as client:  # Local work stays open.
        assert post(client, path, body).status_code == 200


def test_failure_alert_is_sent_after_the_response_only_on_change(make_client):
    with make_client(ingest_token=TOKEN) as client:
        assert post(client, "events", EVENT, TOKEN).status_code == 200
    engine, settings, _session_id = client.alert.call_args.args
    assert engine is client.engine
    assert settings.ingest_token is not None
    with make_client(changed=False, ingest_token=TOKEN) as client:
        assert post(client, "events", EVENT, TOKEN).status_code == 200
    assert client.alert.call_count == 1
