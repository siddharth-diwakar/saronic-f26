import io
import json
import logging

import pytest

from saronic_api.config import Settings
from saronic_api.log import JsonFormatter, _JsonHandler, configure_logging, log_context

DATABASE_URL = "postgresql://team:secret@database:5432/saronic"


@pytest.fixture
def logged():
    """Configure JSON logging and return a reader for the lines it wrote."""
    stream = io.StringIO()
    settings = Settings(_env_file=None, database_url=DATABASE_URL, GIT_COMMIT="abc123")
    configure_logging(settings, stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _JsonHandler)]:
        root.removeHandler(handler)


def test_lines_are_json_tagged_with_commit_env_and_extras(logged):
    logging.getLogger("saronic_api.test").info("boat %s ready", "alpha", extra={"step": "ready"})
    (entry,) = logged()
    assert entry["msg"] == "boat alpha ready"
    assert entry["level"] == "info"
    assert entry["logger"] == "saronic_api.test"
    assert entry["commit"] == "abc123"
    assert entry["env"] == "production"
    assert entry["step"] == "ready"
    assert entry["ts"].endswith("+00:00")


def test_context_tags_lines_and_unwinds(logged):
    logger = logging.getLogger("saronic_api.test")
    with log_context(session_id="s-1", brev_instance_id=None):
        with log_context(brev_instance_id="brev-9"):
            logger.info("inner")
        logger.info("outer")
    logger.info("after")
    inner, outer, after = logged()
    assert (inner["session_id"], inner["brev_instance_id"]) == ("s-1", "brev-9")
    assert outer["session_id"] == "s-1" and "brev_instance_id" not in outer
    assert "session_id" not in after


def test_exceptions_are_kept_in_one_line(logged):
    try:
        raise RuntimeError("provisioning failed")
    except RuntimeError:
        logging.getLogger("saronic_api.test").exception("startup step failed")
    (entry,) = logged()
    assert "RuntimeError: provisioning failed" in entry["exc"]


def test_reconfiguring_does_not_duplicate_lines_and_routes_uvicorn(logged, monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("SOURCE_COMMIT", raising=False)
    replacement = io.StringIO()
    configure_logging(Settings(_env_file=None, database_url=DATABASE_URL), replacement)
    logging.getLogger("uvicorn.error").info("server started")
    assert logged() == []
    (entry,) = [json.loads(line) for line in replacement.getvalue().splitlines()]
    assert entry["logger"] == "uvicorn.error"
    assert entry["commit"] is None
    assert not logging.getLogger("uvicorn.access").handlers


def test_coolify_source_commit_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("SOURCE_COMMIT", "def456")
    assert Settings(_env_file=None, database_url=DATABASE_URL).git_commit == "def456"


def test_formatter_never_emits_database_url():
    config = Settings(_env_file=None, database_url=DATABASE_URL)
    record = logging.makeLogRecord({"msg": "loaded %s", "args": (config,)})
    assert "secret" not in JsonFormatter(commit=None, environment="test").format(record)
