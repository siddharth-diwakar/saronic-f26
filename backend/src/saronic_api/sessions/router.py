"""Session endpoints. Handlers only touch the database; the worker does all Brev calls.

/events and /logs are for the instance to report startup progress and logs.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from saronic_api.auth import require_api_key
from saronic_api.database import get_db
from saronic_api.refs import RefError, RefResolutionError, ResolvedRef, resolve_ref
from saronic_api.sessions.models import LogSource, SimSession, instance_name_for
from saronic_api.sessions.schemas import (
    CreateSessionRequest,
    SessionEventCreate,
    SessionLogCreate,
    SessionResponse,
)
from saronic_api.sessions.service import apply_stop
from saronic_api.sessions.status import ACTIVE_STATUSES, SessionStatus, SessionStep
from saronic_api.sessions.timeline import (
    InvalidSessionInput,
    InvalidSessionTransition,
    apply_reported_step,
    deliver_failure_alert,
    record_log_excerpt,
    sync_timeline,
)

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
)

# Serialises session creation so concurrent POSTs can't both pass the cap check.
_CREATE_LOCK_KEY = 0x5A_51_0001

RefResolver = Callable[[str], ResolvedRef]


def get_ref_resolver(request: Request) -> RefResolver:
    """Resolve session refs against the repository URL from settings."""
    return partial(resolve_ref, repo_url=request.app.state.settings.repo_url)


Db = Annotated[Session, Depends(get_db)]
Resolver = Annotated[RefResolver, Depends(get_ref_resolver)]
SessionId = Annotated[
    uuid.UUID,
    Path(description="Session id returned by POST /api/v1/sessions."),
]
_NOT_FOUND = {404: {"description": "Session not found"}}


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionResponse,
    responses={
        409: {"description": "MAX_ACTIVE_SESSIONS reached"},
        422: {"description": "Invalid, ambiguous or unknown ref"},
        502: {"description": "Could not reach the git repository"},
    },
)
def create_session(
    request: Request,
    db: Db,
    resolve: Resolver,
    body: CreateSessionRequest | None = None,
) -> SessionResponse:
    """Queue a Brev GPU session for a git ref.

    The ref is resolved to a commit before the row is stored. The response is 202;
    the worker creates the instance. 409 when `MAX_ACTIVE_SESSIONS` is already reached,
    422 when the ref is invalid, and 502 when the git host cannot be reached.
    """
    settings = request.app.state.settings
    ref = body.ref if body and body.ref is not None else settings.default_ref
    # Resolve before opening a transaction: this is a network call.
    try:
        resolved = resolve(ref)
    except RefResolutionError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    except RefError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    db.execute(select(func.pg_advisory_xact_lock(_CREATE_LOCK_KEY)))
    active = db.scalar(
        select(func.count())
        .select_from(SimSession)
        .where(SimSession.status.in_([s.value for s in ACTIVE_STATUSES]))
    )
    # DECISION(D2): concurrent sessions allowed, capped by MAX_ACTIVE_SESSIONS (default 1).
    if active >= settings.max_active_sessions:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{active} active session(s); MAX_ACTIVE_SESSIONS is {settings.max_active_sessions}",
        )

    session_id = uuid.uuid4()
    record = SimSession(
        id=session_id,
        status=SessionStatus.REQUESTED.value,
        step=SessionStep.REQUESTED.value,
        ref=resolved.ref,
        commit_sha=resolved.commit_sha,
        instance_name=instance_name_for(session_id),
    )
    sync_timeline(record, datetime.now(UTC))
    db.add(record)
    db.commit()
    return SessionResponse.from_record(record)


@router.get("/{session_id}", response_model=SessionResponse, responses=_NOT_FOUND)
def get_session(session_id: SessionId, db: Db) -> SessionResponse:
    """Return the session, its timeline, and the latest log excerpt for each source."""
    record = db.get(
        SimSession,
        session_id,
        options=[selectinload(SimSession.events), selectinload(SimSession.log_excerpts)],
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionResponse.from_record(record)


@router.post(
    "/{session_id}/stop",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SessionResponse,
    responses=_NOT_FOUND,
)
def stop_session(session_id: SessionId, db: Db) -> SessionResponse:
    """Ask the worker to delete the Brev instance.

    `brev stop` is not used, because a stopped VM still bills for the disk. The session
    stays `stopping` until Brev no longer lists the instance. A `requested` session is
    cancelled immediately and never touches Brev. Repeating stop on a session that is
    already stopping, stopped, or failed changes nothing.
    """
    record = _locked(db, session_id)
    # Stop means delete the Brev instance. `brev stop` would keep billing for the disk.
    # The worker stores a recording log (an upload, or a note that none arrived) before delete.
    apply_stop(record, datetime.now(UTC))
    db.commit()
    return SessionResponse.from_record(record)


@router.post(
    "/{session_id}/events",
    response_model=SessionResponse,
    responses={
        **_NOT_FOUND,
        409: {"description": "Not the next step, or the session is no longer starting"},
        422: {"description": "Invalid step or failure details"},
    },
)
def record_event(
    session_id: SessionId, body: SessionEventCreate, request: Request, db: Db
) -> SessionResponse:
    """Record the next startup step reported by the instance.

    A step that is already stored is a no-op, so the instance can retry the post.
    `failed` stores `failed_step`, `error_code`, and an optional `log_tail`, then moves
    the session to `stopping` so the worker deletes the instance.
    """
    if body.log_tail is not None and body.step is not SessionStep.FAILED:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "log_tail is only accepted when recording failure",
        )
    record = _locked(db, session_id)
    now = datetime.now(UTC)
    try:
        changed = apply_reported_step(
            record,
            body.step,
            now,
            occurred_at=body.occurred_at,
            failed_step=body.failed_step,
            error_code=body.error_code,
            error_detail=body.error_detail,
        )
    except InvalidSessionTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except InvalidSessionInput as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if body.log_tail is not None:
        record_log_excerpt(db, record, source=LogSource.FAILURE_TAIL, body=body.log_tail, now=now)
    db.commit()
    if changed and body.step is SessionStep.FAILED:
        deliver_failure_alert(db, request.app.state.settings, record)
    return SessionResponse.from_record(record)


@router.post("/{session_id}/logs", response_model=SessionResponse, responses=_NOT_FOUND)
def store_log(session_id: SessionId, body: SessionLogCreate, db: Db) -> SessionResponse:
    """Store a capped log excerpt, replacing any earlier excerpt from the same source.

    The body is kept to the last 200 lines or 64 KB. `source` is `bootstrap`,
    `docker_build`, `isaac_launch`, `failure_tail`, or `recording`. A `recording`
    post is what the worker keeps when it deletes the instance.
    """
    record = _locked(db, session_id)
    record_log_excerpt(db, record, source=body.source, body=body.body, now=datetime.now(UTC))
    db.commit()
    return SessionResponse.from_record(record)


def _locked(db: Session, session_id: uuid.UUID) -> SimSession:
    """Return the session row locked for update, or 404 when it does not exist."""
    record = db.scalars(
        select(SimSession).where(SimSession.id == session_id).with_for_update()
    ).one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return record
