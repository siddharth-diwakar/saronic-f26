"""Worker persistence. Each method is its own short transaction; none spans a Brev call."""

import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from saronic_api.sessions.models import SimSession
from saronic_api.sessions.service import SessionRecord, apply_claim
from saronic_api.sessions.status import ACTIVE_STATUSES, MONITORED_STATUSES, SessionStatus


@dataclass(frozen=True)
class ClaimedSession:
    """A session the worker has committed to `provisioning` and is about to create on Brev."""

    id: uuid.UUID
    instance_name: str
    commit_sha: str


class SessionStore(Protocol):
    def claim_requested(self, limit: int, now: datetime) -> list[ClaimedSession]:
        """Move up to `limit` requested sessions to provisioning and commit."""

    def monitored_ids(self) -> list[uuid.UUID]:
        """Sessions the worker should poll Brev for."""

    def owned_instance_names(self) -> set[str]:
        """Instance names of sessions that may still hold a GPU."""

    def locked(self, session_id: uuid.UUID) -> AbstractContextManager[SessionRecord | None]:
        """Row-locked record; changes commit on normal exit and roll back on error."""


class SqlSessionStore:
    """Postgres-backed `SessionStore`. Each method commits its own short transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def claim_requested(self, limit: int, now: datetime) -> list[ClaimedSession]:
        with Session(self._engine) as db:
            # SKIP LOCKED: rows a stop request is holding are left for the next tick.
            records = db.scalars(
                select(SimSession)
                .where(SimSession.status == SessionStatus.REQUESTED.value)
                .order_by(SimSession.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            claimed = []
            for record in records:
                apply_claim(record, now)
                claimed.append(ClaimedSession(record.id, record.instance_name, record.commit_sha))
            db.commit()
        return claimed

    def monitored_ids(self) -> list[uuid.UUID]:
        with Session(self._engine) as db:
            return list(
                db.scalars(
                    select(SimSession.id)
                    .where(SimSession.status.in_([s.value for s in MONITORED_STATUSES]))
                    .order_by(SimSession.created_at)
                )
            )

    def owned_instance_names(self) -> set[str]:
        with Session(self._engine) as db:
            names = db.scalars(
                select(SimSession.instance_name).where(
                    SimSession.status.in_([s.value for s in ACTIVE_STATUSES])
                )
            )
            return set(names)

    @contextmanager
    def locked(self, session_id: uuid.UUID) -> Iterator[SimSession | None]:
        with Session(self._engine) as db:
            yield db.scalars(
                select(SimSession).where(SimSession.id == session_id).with_for_update()
            ).one_or_none()
            db.commit()
