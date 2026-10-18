"""Session timeline routes. Callers commit; uncommitted work rolls back on close."""

import hmac
import logging
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from saronic_api.config import Settings
from saronic_api.database import get_db
from saronic_api.log import log_context
from saronic_api.models import SessionStep
from saronic_api.schemas import (
    SessionAttemptCreate,
    SessionCreate,
    SessionEventCreate,
    SessionLogCreate,
    SessionOut,
    to_session_out,
)
from saronic_api.timeline import (
    InvalidSessionInput,
    InvalidSessionTransition,
    SessionNotFound,
    apply_step,
    deliver_failure_alert,
    load_session,
    open_session,
    record_log,
    start_attempt,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


Db = Annotated[Session, Depends(get_db)]
CurrentSettings = Annotated[Settings, Depends(get_settings)]


def require_ingest_token(
    settings: CurrentSettings, x_ingest_token: Annotated[str | None, Header()] = None
) -> None:
    """Brev instances report over the internet, so their routes need the shared token."""
    expected = settings.ingest_token
    if expected is None:
        if settings.app_env == "production":
            raise HTTPException(status_code=503, detail="Instance reporting is not configured")
        return
    if x_ingest_token is None or not hmac.compare_digest(
        x_ingest_token.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid ingest token")


InstanceReport = Depends(require_ingest_token)


@router.post("", response_model=SessionOut, status_code=201)
def create_session(body: SessionCreate, db: Db) -> SessionOut:
    with _session_errors():
        row = open_session(db, git_commit=body.git_commit, brev_instance_id=body.brev_instance_id)
    return _commit_detail(db, row.id)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: UUID, db: Db) -> SessionOut:
    return _detail(db, session_id)


@router.post("/{session_id}/events", response_model=SessionOut, dependencies=[InstanceReport])
def record_event(
    session_id: UUID,
    body: SessionEventCreate,
    request: Request,
    background: BackgroundTasks,
    db: Db,
    settings: CurrentSettings,
) -> SessionOut:
    with _session_errors():
        result = apply_step(
            db,
            session_id,
            body.step,
            brev_instance_id=body.brev_instance_id,
            error_code=body.error_code,
            error_detail=body.error_detail,
            failed_step=body.failed_step,
            occurred_at=body.occurred_at,
            log_tail=body.log_tail,
        )
    db.commit()
    with log_context(session_id=str(session_id), brev_instance_id=body.brev_instance_id):
        log = logger.error if result.status is SessionStep.FAILED else logger.info
        log(
            "Session step recorded",
            extra={
                "step": body.step.value,
                "status": result.status.value,
                "changed": result.changed,
            },
        )
    if result.changed and result.status is SessionStep.FAILED:
        # After the response: a slow webhook must not hold up the reporting instance.
        background.add_task(_send_failure_alert, request.app.state.engine, settings, session_id)
    return _detail(db, session_id)


def _send_failure_alert(engine: Engine, settings: Settings, session_id: UUID) -> None:
    # The request's session is closed by now; the alert claim needs its own transaction.
    with Session(engine) as db:
        failed = load_session(db, session_id)
        if failed is not None:
            deliver_failure_alert(db, settings, failed)


@router.post("/{session_id}/logs", response_model=SessionOut, dependencies=[InstanceReport])
def store_log(session_id: UUID, body: SessionLogCreate, db: Db) -> SessionOut:
    with _session_errors():
        record_log(db, session_id, source=body.source, body=body.body, attempt=body.attempt)
    return _commit_detail(db, session_id)


@router.post("/{session_id}/attempts", response_model=SessionOut)
def retry_session(session_id: UUID, body: SessionAttemptCreate, db: Db) -> SessionOut:
    with _session_errors():
        start_attempt(db, session_id, git_commit=body.git_commit)
    return _commit_detail(db, session_id)


def _commit_detail(db: Session, session_id: UUID) -> SessionOut:
    db.commit()
    return _detail(db, session_id)


def _detail(db: Session, session_id: UUID) -> SessionOut:
    loaded = load_session(db, session_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return to_session_out(loaded)


@contextmanager
def _session_errors():
    try:
        yield
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="Session not found") from None
    except InvalidSessionTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidSessionInput as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
