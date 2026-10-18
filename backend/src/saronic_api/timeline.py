"""Session steps, capped log excerpts, and Slack alerts.

A step change updates the session row and inserts one event in the caller's
transaction. Reaching ready, failed, or stopped does not delete earlier events.
Stopping a live session appends teardown_requested and then stopped. A retry
opens another attempt and leaves the failed attempt in the timeline.
"""

import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from saronic_api.config import Settings
from saronic_api.models import (
    FORWARD_STEPS,
    IN_PROGRESS_STEPS,
    UQ_LOG_EXCERPT,
    UQ_SESSION_ALERT,
    AlertKind,
    LogSource,
    SessionAlert,
    SessionEvent,
    SessionLogExcerpt,
    SessionStep,
    SimulationSession,
)

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 200
MAX_LOG_BYTES = 64 * 1024
MAX_ERROR_CODE_LENGTH = 64
MAX_ERROR_DETAIL_LENGTH = 2000


class SessionNotFound(Exception):
    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        super().__init__(f"Session {session_id} not found")


class InvalidSessionTransition(Exception):
    def __init__(self, status: SessionStep, step: SessionStep):
        self.status = status
        self.step = step
        super().__init__(f"Cannot move session from {status.value} to {step.value}")


class InvalidSessionInput(Exception):
    pass


@dataclass(frozen=True)
class TransitionPlan:
    noop: bool
    status: SessionStep
    failed_step: SessionStep | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class StepResult:
    session_id: uuid.UUID
    changed: bool
    status: SessionStep


def plan_transition(
    status: SessionStep,
    step: SessionStep,
    *,
    failed_step: SessionStep | None,
    error_code: str | None,
    error_detail: str | None,
) -> TransitionPlan:
    if step is status:
        return TransitionPlan(noop=True, status=status)
    if step is not SessionStep.FAILED and (failed_step is not None or error_code or error_detail):
        raise InvalidSessionInput(
            "error_code, error_detail, and failed_step are only recorded on failure"
        )
    if _is_next_forward_step(status, step):
        return TransitionPlan(noop=False, status=step)
    if step is SessionStep.FAILED and _can_fail(status):
        checked_step, code, detail = _validate_failure(failed_step, error_code, error_detail)
        return TransitionPlan(
            noop=False,
            status=step,
            failed_step=checked_step,
            error_code=code,
            error_detail=detail,
        )
    if step is SessionStep.TEARDOWN_REQUESTED and _can_teardown(status):
        return TransitionPlan(noop=False, status=step)
    if step is SessionStep.STOPPED and status in (
        SessionStep.TEARDOWN_REQUESTED,
        SessionStep.FAILED,
    ):
        return TransitionPlan(noop=False, status=step)
    raise InvalidSessionTransition(status, step)


def plan_retry(status: SessionStep) -> None:
    if status not in (SessionStep.FAILED, SessionStep.STOPPED):
        raise InvalidSessionTransition(status, SessionStep.REQUESTED)


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


def open_session(
    db: Session, *, git_commit: str, brev_instance_id: str | None
) -> SimulationSession:
    row = SimulationSession(
        status=SessionStep.REQUESTED.value,
        attempt=1,
        git_commit=git_commit,
        brev_instance_id=brev_instance_id,
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    _add_event(
        db,
        row,
        step=SessionStep.REQUESTED,
        occurred_at=row.created_at,
        error_code=None,
        error_detail=None,
        failed_step=None,
    )
    db.flush()
    return row


def apply_step(
    db: Session,
    session_id: uuid.UUID,
    step: SessionStep,
    *,
    brev_instance_id: str | None,
    error_code: str | None,
    error_detail: str | None,
    failed_step: SessionStep | None,
    occurred_at: datetime | None,
    log_tail: str | None,
) -> StepResult:
    row = _locked_session(db, session_id)
    plan = plan_transition(
        SessionStep(row.status),
        step,
        failed_step=failed_step,
        error_code=error_code,
        error_detail=error_detail,
    )
    if log_tail is not None and step is not SessionStep.FAILED:
        raise InvalidSessionInput("log_tail is only accepted when recording failure")
    if brev_instance_id:
        row.brev_instance_id = brev_instance_id
    if not plan.noop:
        row.status = plan.status.value
        if plan.status is SessionStep.FAILED:
            row.failed_step = plan.failed_step.value if plan.failed_step else None
            row.error_code = plan.error_code
            row.error_detail = plan.error_detail
        _add_event(
            db,
            row,
            step=step,
            occurred_at=occurred_at or datetime.now(UTC),
            error_code=plan.error_code,
            error_detail=plan.error_detail,
            failed_step=plan.failed_step,
        )
    if log_tail is not None:
        record_log_excerpt(
            db, row, source=LogSource.FAILURE_TAIL, body=log_tail, attempt=row.attempt
        )
    db.flush()
    return StepResult(session_id=row.id, changed=not plan.noop, status=SessionStep(row.status))


def start_attempt(db: Session, session_id: uuid.UUID, *, git_commit: str) -> SimulationSession:
    row = _locked_session(db, session_id)
    plan_retry(SessionStep(row.status))
    row.attempt += 1
    row.status = SessionStep.REQUESTED.value
    row.git_commit = git_commit
    row.brev_instance_id = None
    row.failed_step = None
    row.error_code = None
    row.error_detail = None
    _add_event(
        db,
        row,
        step=SessionStep.REQUESTED,
        occurred_at=datetime.now(UTC),
        error_code=None,
        error_detail=None,
        failed_step=None,
    )
    db.flush()
    return row


def record_log(
    db: Session,
    session_id: uuid.UUID,
    *,
    source: LogSource,
    body: str,
    attempt: int | None,
) -> None:
    row = _locked_session(db, session_id)
    record_log_excerpt(db, row, source=source, body=body, attempt=attempt)
    db.flush()


def record_log_excerpt(
    db: Session,
    row: SimulationSession,
    *,
    source: LogSource,
    body: str,
    attempt: int | None,
) -> None:
    target_attempt = row.attempt if attempt is None else attempt
    if target_attempt < 1 or target_attempt > row.attempt:
        raise InvalidSessionInput("attempt is outside this session's history")
    text, line_count, truncated = cap_log(body)
    stmt = pg_insert(SessionLogExcerpt).values(
        session_id=row.id,
        attempt=target_attempt,
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
    if target_attempt == row.attempt:
        row.updated_at = datetime.now(UTC)


def load_session(db: Session, session_id: uuid.UUID) -> SimulationSession | None:
    stmt = (
        select(SimulationSession)
        .where(SimulationSession.id == session_id)
        .options(
            selectinload(SimulationSession.events),
            selectinload(SimulationSession.log_excerpts),
        )
    )
    return db.scalar(stmt)


def find_stuck_sessions(db: Session, *, older_than: datetime) -> list[SimulationSession]:
    stmt = select(SimulationSession).where(
        SimulationSession.status.in_([step.value for step in IN_PROGRESS_STEPS]),
        SimulationSession.updated_at < older_than,
    )
    return list(db.scalars(stmt))


def failure_alert_text(row: SimulationSession) -> str:
    failed = row.failed_step or row.status
    instance = row.brev_instance_id or "no instance"
    detail = f" {row.error_detail}" if row.error_detail else ""
    return (
        f"Session {row.id} failed at {failed} ({row.error_code}) "
        f"on {instance}, commit {row.git_commit}.{detail}"
    )


def stuck_alert_text(row: SimulationSession) -> str:
    instance = row.brev_instance_id or "no instance"
    updated = row.updated_at.isoformat() if row.updated_at is not None else "unknown"
    return (
        f"Session {row.id} is still {row.status} on {instance}, "
        f"commit {row.git_commit}. Last update {updated}."
    )


def deliver_failure_alert(db: Session, settings: Settings, row: SimulationSession) -> None:
    """Claim the alert, then send it. A failed send removes the claim."""
    webhook = _webhook(settings)
    if webhook is None:
        return
    step = row.failed_step or row.status
    if not _claim_alert(db, row, kind=AlertKind.FAILED, step=step):
        return
    db.commit()
    try:
        post_slack(webhook, failure_alert_text(row))
    except (OSError, urllib.error.URLError):
        logger.warning("Slack failure alert was not delivered for session %s", row.id)
        _release_alert(db, row, kind=AlertKind.FAILED, step=step)
        db.commit()


def deliver_stuck_alerts(db: Session, settings: Settings, *, older_than: datetime) -> int:
    """Alert on sessions still provisioning. Nothing calls this on a schedule yet."""
    webhook = _webhook(settings)
    if webhook is None:
        return 0
    sent = 0
    for row in find_stuck_sessions(db, older_than=older_than):
        if not _claim_alert(db, row, kind=AlertKind.STUCK, step=row.status):
            continue
        db.commit()
        try:
            post_slack(webhook, stuck_alert_text(row))
        except (OSError, urllib.error.URLError):
            logger.warning("Slack stuck alert was not delivered for session %s", row.id)
            _release_alert(db, row, kind=AlertKind.STUCK, step=row.status)
            db.commit()
            continue
        sent += 1
    return sent


def post_slack(webhook_url: str, text: str) -> None:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 300:
            raise OSError(f"Slack webhook returned {response.status}")


def _locked_session(db: Session, session_id: uuid.UUID) -> SimulationSession:
    row = db.get(SimulationSession, session_id, with_for_update=True)
    if row is None:
        raise SessionNotFound(session_id)
    return row


def _add_event(
    db: Session,
    row: SimulationSession,
    *,
    step: SessionStep,
    occurred_at: datetime,
    error_code: str | None,
    error_detail: str | None,
    failed_step: SessionStep | None,
) -> None:
    db.add(
        SessionEvent(
            session_id=row.id,
            attempt=row.attempt,
            step=step.value,
            occurred_at=occurred_at,
            brev_instance_id=row.brev_instance_id,
            git_commit=row.git_commit,
            error_code=error_code,
            error_detail=error_detail,
            failed_step=failed_step.value if failed_step is not None else None,
        )
    )


def _is_next_forward_step(status: SessionStep, step: SessionStep) -> bool:
    if status not in FORWARD_STEPS or step not in FORWARD_STEPS:
        return False
    return FORWARD_STEPS.index(step) == FORWARD_STEPS.index(status) + 1


def _can_fail(status: SessionStep) -> bool:
    return status in IN_PROGRESS_STEPS or status is SessionStep.TEARDOWN_REQUESTED


def _can_teardown(status: SessionStep) -> bool:
    return status in FORWARD_STEPS or status is SessionStep.FAILED


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


def _claim_alert(db: Session, row: SimulationSession, *, kind: AlertKind, step: str) -> bool:
    stmt = (
        pg_insert(SessionAlert)
        .values(
            session_id=row.id,
            attempt=row.attempt,
            kind=kind.value,
            step=step,
        )
        .on_conflict_do_nothing(constraint=UQ_SESSION_ALERT)
        .returning(SessionAlert.id)
    )
    return db.execute(stmt).first() is not None


def _release_alert(db: Session, row: SimulationSession, *, kind: AlertKind, step: str) -> None:
    db.execute(
        delete(SessionAlert).where(
            SessionAlert.session_id == row.id,
            SessionAlert.attempt == row.attempt,
            SessionAlert.kind == kind.value,
            SessionAlert.step == step,
        )
    )
