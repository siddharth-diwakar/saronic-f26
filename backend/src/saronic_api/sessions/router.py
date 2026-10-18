"""Session endpoints. Handlers only touch the database; the worker does all Brev calls."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from saronic_api.auth import require_api_key
from saronic_api.database import get_db
from saronic_api.refs import RefError, RefResolutionError, ResolvedRef, resolve_ref
from saronic_api.sessions.models import SimSession, instance_name_for
from saronic_api.sessions.schemas import CreateSessionRequest, SessionResponse
from saronic_api.sessions.service import apply_stop
from saronic_api.sessions.status import ACTIVE_STATUSES, SessionStatus

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_key)],
)

# Serialises session creation so concurrent POSTs can't both pass the cap check.
_CREATE_LOCK_KEY = 0x5A_51_0001

RefResolver = Callable[[str], ResolvedRef]


def get_ref_resolver(request: Request) -> RefResolver:
    return partial(resolve_ref, repo_url=request.app.state.settings.repo_url)


Db = Annotated[Session, Depends(get_db)]
Resolver = Annotated[RefResolver, Depends(get_ref_resolver)]


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
        ref=resolved.ref,
        commit_sha=resolved.commit_sha,
        instance_name=instance_name_for(session_id),
    )
    db.add(record)
    db.commit()
    return SessionResponse.from_record(record)


@router.get("/{session_id}", response_model=SessionResponse)
def get_session(session_id: uuid.UUID, db: Db) -> SessionResponse:
    record = db.get(SimSession, session_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return SessionResponse.from_record(record)


@router.post(
    "/{session_id}/stop", status_code=status.HTTP_202_ACCEPTED, response_model=SessionResponse
)
def stop_session(session_id: uuid.UUID, db: Db) -> SessionResponse:
    record = db.scalars(
        select(SimSession).where(SimSession.id == session_id).with_for_update()
    ).one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    # DECISION(D1): stop means delete the Brev instance (the worker does it from `stopping`);
    # `brev stop` would keep billing for the disk.
    # DECISION(D3): no output saving yet; `stopping` is where Eddie's save step will hook in.
    apply_stop(record, datetime.now(UTC))
    db.commit()
    return SessionResponse.from_record(record)
