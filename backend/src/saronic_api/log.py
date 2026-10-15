"""Structured JSON logs; one object per line, tagged with deploy and session context."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TextIO

from saronic_api.config import Settings

_context: ContextVar[dict[str, str] | None] = ContextVar("log_context", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_RECORD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@contextmanager
def log_context(**fields: str | None) -> Iterator[None]:
    """Tag every log line inside the block, e.g. session_id and brev_instance_id.

    Context follows the current task or thread, so concurrent requests and
    worker jobs never see each other's fields. None values are skipped.
    """
    added = {key: str(value) for key, value in fields.items() if value is not None}
    token = _context.set({**(_context.get() or {}), **added})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, *, commit: str | None, environment: str) -> None:
        super().__init__()
        self._static = {"env": environment, "commit": commit}

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **self._static,
            **(_context.get() or {}),
        }
        entry.update(
            (key, value) for key, value in vars(record).items() if key not in _RECORD_ATTRS
        )
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class _JsonHandler(logging.StreamHandler):
    """Marker type so reconfiguring replaces only our handler, not pytest's or others'."""


def configure_logging(settings: Settings, stream: TextIO = sys.stdout) -> None:
    formatter = JsonFormatter(commit=settings.git_commit, environment=settings.app_env)
    handler = _JsonHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, _JsonHandler)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Route uvicorn's server and access logs through the same JSON handler.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
