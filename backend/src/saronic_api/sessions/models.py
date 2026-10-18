"""sim_sessions: one row per on-demand Brev GPU session.

DECISION(D6): no session model existed on main or any open branch on 2026-10-15, so this
is the §6.3 schema from the PRD. Flagged for Eddie's review.
"""

import uuid
from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from saronic_api.database import Base
from saronic_api.sessions.status import SessionStatus

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in SessionStatus)


def instance_name_for(session_id: uuid.UUID) -> str:
    # Derived from the ID so a retried create for the same session hits "already exists".
    return f"saronic-{session_id.hex[:12]}"


class SimSession(Base):
    __tablename__ = "sim_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_sim_sessions_status"),
        CheckConstraint("commit_sha ~ '^[0-9a-f]{40}$'", name="ck_sim_sessions_commit_sha"),
        UniqueConstraint("instance_name", name="uq_sim_sessions_instance_name"),
        Index("ix_sim_sessions_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(CHAR(40))
    instance_name: Mapped[str] = mapped_column(Text)
    instance_id: Mapped[str | None] = mapped_column(Text)

    # Raw values from the last successful `brev ls`; unknown strings are stored as-is.
    brev_status: Mapped[str | None] = mapped_column(Text)
    brev_build_status: Mapped[str | None] = mapped_column(Text)
    brev_health_status: Mapped[str | None] = mapped_column(Text)
    brev_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
