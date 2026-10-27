"""Session timeline: step events, capped log excerpts, and Slack alerts.

The backend records requested, provisioning, instance_up, failed, teardown_requested and
stopped from the worker-facing fields (sync_timeline). The instance reports
container_built through ready, or a failure, through POST /events (apply_reported_step).

Each step is recorded at most once per session, and every writer holds the session's
row lock, so a repeated step is a no-op rather than a duplicate. Reaching ready, failed
or stopped never deletes earlier events.
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from saronic_api.config import Settings
from saronic_api.sessions.models import (
    UQ_LOG_EXCERPT,
    UQ_SESSION_ALERT,
    AlertKind,
    LogSource,
    SessionAlert,
    SessionEvent,
    SessionLogExcerpt,
    SimSession,
)
from saronic_api.sessions.status import (
    FORWARD_STEPS,
    IN_PROGRESS_STEPS,
    INSTANCE_STEPS,
    TERMINAL_STATUSES,
    SessionStatus,
    SessionStep,
)

if TYPE_CHECKING:
    from saronic_api.sessions.service import SessionRecord

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 200
MAX_LOG_BYTES = 64 * 1024
MAX_ERROR_CODE_LENGTH = 64
MAX_ERROR_DETAIL_LENGTH = 2000

# Statuses in which the instance may still be starting, so its reports are accepted.
_REPORTING_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.RUNNING})


class InvalidSessionTransition(Exception):
    """The step is valid but not next for this session (HTTP 409)."""


class InvalidSessionInput(Exception):
    """The request itself is invalid (HTTP 422)."""


def record_step(
    record: "SessionRecord",
    step: SessionStep,
    occurred_at: datetime,
    *,
    failed_step: SessionStep | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """Append `step` unless it is already recorded. Returns whether it was appended."""
    if has_step(record, step):
        return False
    record.events.append(
        SessionEvent(
            step=step.value,
            occurred_at=occurred_at,
            brev_instance_id=record.instance_id,
            git_commit=record.commit_sha,
            error_code=error_code,
            error_detail=error_detail,
            failed_step=failed_step.value if failed_step is not None else None,
        )
    )
    current = SessionStep(record.step)
    # Forward steps never move the session back, e.g. a late instance_up after the
    # instance already reported container_built.
    if step not in FORWARD_STEPS or (
        current in FORWARD_STEPS and FORWARD_STEPS.index(step) > FORWARD_STEPS.index(current)
    ):
        record.step = step.value
    if step is SessionStep.FAILED:
        record.failed_step = failed_step.value if failed_step is not None else None
    return True


def has_step(record: "SessionRecord", step: SessionStep) -> bool:
    """True when this session already has an event for `step`."""
    return any(event.step == step.value for event in record.events)


def sync_timeline(record: "SessionRecord", now: datetime) -> None:
    """Record the steps implied by the worker-facing fields. Safe to call after any change."""
    status = SessionStatus(record.status)
    record_step(record, SessionStep.REQUESTED, now)
    if record.provisioning_started_at is not None:
        record_step(record, SessionStep.PROVISIONING, now)
    if record.running_at is not None:
        record_step(record, SessionStep.INSTANCE_UP, now)
    if record.error_code is not None:
        message = record.error_message
        record_step(
            record,
            SessionStep.FAILED,
            now,
            failed_step=_failing_step(SessionStep(record.step)),
            error_code=record.error_code[:MAX_ERROR_CODE_LENGTH],
            error_detail=message[:MAX_ERROR_DETAIL_LENGTH] if message else None,
        )
    if status in (SessionStatus.STOPPING, *TERMINAL_STATUSES):
        record_step(record, SessionStep.TEARDOWN_REQUESTED, now)
    if status in TERMINAL_STATUSES:
        record_step(record, SessionStep.STOPPED, now)


def apply_reported_step(
    record: "SessionRecord",
    step: SessionStep,
    now: datetime,
    *,
    occurred_at: datetime | None = None,
    failed_step: SessionStep | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> bool:
    """Apply a step the instance reported. Returns False when it was already recorded.

    A reported failure sets the session's error and moves it to stopping, so the worker
    deletes the instance like any other failure.
    """
    if step not in INSTANCE_STEPS and step is not SessionStep.FAILED:
        raise InvalidSessionInput(f"{step.value} is recorded by the backend, not reported")
    if step is not SessionStep.FAILED and (failed_step is not None or error_code or error_detail):
        raise InvalidSessionInput(
            "error_code, error_detail, and failed_step are only recorded on failure"
        )
    if has_step(record, step):
        return False  # a retried report
    status = SessionStatus(record.status)
    if status not in _REPORTING_STATUSES:
        raise InvalidSessionTransition(f"Cannot record {step.value}: the session is {status.value}")
    at = occurred_at or now

    if step is SessionStep.FAILED:
        checked, code, detail = _validate_failure(failed_step, error_code, error_detail)
        record_step(record, step, at, failed_step=checked, error_code=code, error_detail=detail)
        record.error_code = code
        record.error_message = detail
        record.status = SessionStatus.STOPPING.value
        record.last_activity_at = now
        sync_timeline(record, now)
        return True

    current = SessionStep(record.step)
    # The instance can report before the worker's next poll sees Brev RUNNING.
    if current is SessionStep.PROVISIONING and step is SessionStep.CONTAINER_BUILT:
        record_step(record, SessionStep.INSTANCE_UP, at)
        current = SessionStep.INSTANCE_UP
    if (
        current not in FORWARD_STEPS
        or FORWARD_STEPS.index(step) != FORWARD_STEPS.index(current) + 1
    ):
        raise InvalidSessionTransition(
            f"Cannot record {step.value}: the session is at {current.value}"
        )
    record_step(record, step, at)
    record.last_activity_at = now
    return True


def cap_log(body: str) -> tuple[str, int, bool]:
    """Keep the tail: at most 200 lines and 64 KB."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return "", 0, False
    lines = normalized.split("\n")
    truncated = False
    if len(lines) > MAX_LOG_LINES:
        lines = lines[-MAX_LOG_LINES:]
        truncated = True
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_LOG_BYTES:
        text = encoded[-MAX_LOG_BYTES:].decode("utf-8", errors="ignore")
        if "\n" in text:
            text = text.split("\n", 1)[1]
        truncated = True
    if not text:
        return "", 0, True
    return text, text.count("\n") + 1, truncated


def record_log_excerpt(
    db: Session, record: SimSession, *, source: LogSource, body: str, now: datetime
) -> None:
    """Store the capped tail of a log, replacing any earlier excerpt from the same source."""
    text, line_count, truncated = cap_log(body)
    stmt = pg_insert(SessionLogExcerpt).values(
        session_id=record.id,
        source=source.value,
        body=text,
        line_count=line_count,
        truncated=truncated,
    )
    stmt = stmt.on_conflict_do_update(
        constraint=UQ_LOG_EXCERPT,
        set_={
            "body": stmt.excluded.body,
            "line_count": stmt.excluded.line_count,
            "truncated": stmt.excluded.truncated,
            "recorded_at": func.now(),
        },
    )
    db.execute(stmt)
    record.updated_at = now
    record.last_activity_at = now
    if source is LogSource.RECORDING:
        record.outputs_saved_at = now


def ensure_recording_saved(record: "SessionRecord", now: datetime) -> None:
    """Keep a recording log before the instance is deleted.

    An upload via POST /logs is left as-is. Otherwise a short note is stored so the
    delete still has a record of what was (not) saved.
    """
    if record.outputs_saved_at is not None:
        return
    excerpts = record.log_excerpts
    if not any(excerpt.source == LogSource.RECORDING.value for excerpt in excerpts):
        body = (
            f"No recording uploaded for {record.instance_name} before the instance was deleted.\n"
        )
        if isinstance(record, SimSession):
            excerpts.append(
                SessionLogExcerpt(
                    session_id=record.id,
                    source=LogSource.RECORDING.value,
                    body=body,
                    line_count=1,
                    truncated=False,
                    recorded_at=now,
                )
            )
        else:
            excerpts.append(
                SimpleNamespace(
                    source=LogSource.RECORDING.value,
                    body=body,
                    line_count=1,
                    truncated=False,
                    recorded_at=now,
                )
            )
    record.outputs_saved_at = now


def find_stuck_sessions(db: Session, *, older_than: datetime) -> list[SimSession]:
    """Active sessions short of ready whose latest event is older than `older_than`."""
    last_event = (
        select(func.max(SessionEvent.occurred_at))
        .where(SessionEvent.session_id == SimSession.id)
        .scalar_subquery()
    )
    stmt = select(SimSession).where(
        SimSession.status.in_([s.value for s in (SessionStatus.REQUESTED, *_REPORTING_STATUSES)]),
        SimSession.step.in_([step.value for step in IN_PROGRESS_STEPS]),
        last_event < older_than,
    )
    return list(db.scalars(stmt))


def failure_alert_text(record: SimSession) -> str:
    """Slack text for a failed session. Log bodies are not included."""
    failed = record.failed_step or record.step
    instance = record.instance_id or record.instance_name
    detail = f" {record.error_message}" if record.error_message else ""
    return (
        f"Session {record.id} failed at {failed} ({record.error_code}) "
        f"on {instance}, commit {record.commit_sha}.{detail}"
    )


def stuck_alert_text(record: SimSession) -> str:
    """Slack text for a session that has not advanced a step."""
    instance = record.instance_id or record.instance_name
    updated = record.updated_at.isoformat() if record.updated_at is not None else "unknown"
    return (
        f"Session {record.id} is still {record.step} on {instance}, "
        f"commit {record.commit_sha}. Last update {updated}."
    )


def deliver_failure_alert(db: Session, settings: Settings, record: SimSession) -> None:
    """Claim the alert, then send it. A failed send removes the claim."""
    webhook = _webhook(settings)
    if webhook is None:
        return
    step = record.failed_step or record.step
    if not _claim_alert(db, record, kind=AlertKind.FAILED, step=step):
        return
    db.commit()
    try:
        post_slack(webhook, failure_alert_text(record))
    except (OSError, urllib.error.URLError):
        logger.warning("Slack failure alert was not delivered for session %s", record.id)
        _release_alert(db, record, kind=AlertKind.FAILED, step=step)
        db.commit()


def deliver_stuck_alerts(db: Session, settings: Settings, *, older_than: datetime) -> int:
    """Alert on sessions still starting. Nothing calls this on a schedule yet."""
    webhook = _webhook(settings)
    if webhook is None:
        return 0
    sent = 0
    for record in find_stuck_sessions(db, older_than=older_than):
        step = record.step
        if not _claim_alert(db, record, kind=AlertKind.STUCK, step=step):
            continue
        db.commit()
        try:
            post_slack(webhook, stuck_alert_text(record))
        except (OSError, urllib.error.URLError):
            logger.warning("Slack stuck alert was not delivered for session %s", record.id)
            _release_alert(db, record, kind=AlertKind.STUCK, step=step)
            db.commit()
            continue
        sent += 1
    return sent


def post_slack(webhook_url: str, text: str) -> None:
    """POST a Slack incoming-webhook payload. Raises if the webhook does not accept it."""
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 300:
            raise OSError(f"Slack webhook returned {response.status}")


def _failing_step(current: SessionStep) -> SessionStep | None:
    """The step a failure interrupted: the one after `current`."""
    if current not in FORWARD_STEPS:
        return None
    if current is SessionStep.READY:
        return SessionStep.READY
    return FORWARD_STEPS[FORWARD_STEPS.index(current) + 1]


def _validate_failure(
    failed_step: SessionStep | None, error_code: str | None, error_detail: str | None
) -> tuple[SessionStep, str, str | None]:
    if failed_step not in FORWARD_STEPS:
        raise InvalidSessionInput("failed_step must be a pipeline step")
    if error_code is None or not error_code.strip():
        raise InvalidSessionInput("error_code is required when a session fails")
    code = error_code.strip()
    if len(code) > MAX_ERROR_CODE_LENGTH:
        raise InvalidSessionInput("error_code is too long")
    detail = None if error_detail is None else error_detail.strip() or None
    if detail is not None and len(detail) > MAX_ERROR_DETAIL_LENGTH:
        raise InvalidSessionInput("error_detail is too long")
    return failed_step, code, detail


def _webhook(settings: Settings) -> str | None:
    if settings.slack_webhook_url is None:
        return None
    return settings.slack_webhook_url.get_secret_value()


def _claim_alert(db: Session, record: SimSession, *, kind: AlertKind, step: str) -> bool:
    stmt = (
        pg_insert(SessionAlert)
        .values(session_id=record.id, kind=kind.value, step=step)
        .on_conflict_do_nothing(constraint=UQ_SESSION_ALERT)
        .returning(SessionAlert.id)
    )
    return db.execute(stmt).first() is not None


def _release_alert(db: Session, record: SimSession, *, kind: AlertKind, step: str) -> None:
    db.execute(
        delete(SessionAlert).where(
            SessionAlert.session_id == record.id,
            SessionAlert.kind == kind.value,
            SessionAlert.step == step,
        )
    )
