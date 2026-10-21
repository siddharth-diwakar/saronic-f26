from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from saronic_api.config import Settings
from saronic_api.database import database_url
from saronic_api.main import create_app

SESSION_PATHS = {
    "/api/v1/sessions",
    "/api/v1/sessions/{session_id}",
    "/api/v1/sessions/{session_id}/stop",
    "/api/v1/sessions/{session_id}/events",
    "/api/v1/sessions/{session_id}/logs",
}


def settings():
    # app_env defaults to production, which requires an API key.
    return Settings(
        _env_file=None,
        database_url="postgresql://team:secret@database:5432/saronic",
        SARONIC_API_KEY="test-key",
    )


def test_coolify_url_selects_psycopg_and_preserves_credentials():
    config = Settings(
        _env_file=None, database_url="postgres://team:p%40ss%25@database:5432/saronic"
    )
    url = database_url(config)
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@ss%"
    assert "p@ss%" not in repr(config)


def test_missing_database_configuration_fails(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_lifecycle_checks_database_and_releases_pool_and_exposes_session_routes(monkeypatch):
    engine = Mock()
    check = Mock()
    monkeypatch.setattr("saronic_api.main.build_engine", lambda _: engine)
    monkeypatch.setattr("saronic_api.main.check_database", check)
    with TestClient(create_app(settings())) as client:
        check.assert_called_once_with(engine)
        assert set(client.get("/openapi.json").json()["paths"]) == SESSION_PATHS
        assert client.get("/").status_code == 404
        assert client.get("/sessions").status_code == 404
    engine.dispose.assert_called_once()


def test_database_failure_prevents_startup_and_disposes_pool(monkeypatch):
    engine = Mock()
    monkeypatch.setattr("saronic_api.main.build_engine", lambda _: engine)
    monkeypatch.setattr(
        "saronic_api.main.check_database", Mock(side_effect=RuntimeError("DB unavailable"))
    )
    with (
        pytest.raises(RuntimeError, match="DB unavailable"),
        TestClient(create_app(settings())),
    ):
        pass
    engine.dispose.assert_called_once()
