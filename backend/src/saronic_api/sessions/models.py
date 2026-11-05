"""sim_sessions: one row per on-demand Brev GPU session, plus its timeline.

The session row holds the worker's `status` and the latest timeline `step`. Events,
log excerpts and alerts hang off it and are deleted with it. A retry is a new session
(instance names are derived from the session ID), so there are no attempts.
"""

import enum
import re
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saronic_api.database import Base
from saronic_api.sessions.status import FORWARD_STEPS, SessionStatus, SessionStep

UQ_SESSION_EVENT = "uq_session_events_session_step"
UQ_LOG_EXCERPT = "uq_session_log_excerpts_session_source"
UQ_SESSION_ALERT = "uq_session_alerts_session_kind_step"


class LogSource(enum.StrEnum):
    """Where a capped log excerpt came from. One row per source per session."""

    BOOTSTRAP = "bootstrap"
    DOCKER_BUILD = "docker_build"
    ISAAC_LAUNCH = "isaac_launch"
    FAILURE_TAIL = "failure_tail"
    RECORDING = "recording"


class AlertKind(enum.StrEnum):
    """Why a Slack alert was sent. Unique with the step so a retry does not post twice."""

    FAILED = "failed"
    STUCK = "stuck"


def _sql_list(values: Iterable[enum.StrEnum]) -> str:
    return ", ".join(f"'{value.value}'" for value in values)


STATUS_SQL = f"status IN ({_sql_list(SessionStatus)})"
STEP_SQL = f"step IN ({_sql_list(SessionStep)})"
FAILED_STEP_SQL = f"failed_step IS NULL OR failed_step IN ({_sql_list(FORWARD_STEPS)})"
LOG_SOURCE_SQL = f"source IN ({_sql_list(LogSource)})"
ALERT_KIND_SQL = f"kind IN ({_sql_list(AlertKind)})"


def instance_name_for(session_id: uuid.UUID) -> str:
    """Brev name derived from the session id, so a retried create hits "already exists"."""
    return f"saronic-{session_id.hex[:12]}"


# Our instances, plus a numeric suffix Brev appends when a retried create mints a second one.
_MANAGED_INSTANCE = re.compile(r"^saronic-[0-9a-f]{12}(?:-\d+)?$")


def is_managed_instance_name(name: str) -> bool:
    """True for names this backend creates, including a numeric suffix from a duplicate create."""
    return _MANAGED_INSTANCE.fullmatch(name) is not None


class SimSession(Base):
    """One on-demand Brev GPU session and the worker's current view of it.

    ``status`` drives the worker. ``step`` is the latest timeline step. ``last_activity_at``
    moves when the instance reports a step or a log, and is what the idle timeout reads.
    ``outputs_saved_at`` is set once a recording log is stored, which is required before delete.
    """

    __tablename__ = "sim_sessions"
    __table_args__ = (
        CheckConstraint(STATUS_SQL, name="ck_sim_sessions_status"),
        CheckConstraint(STEP_SQL, name="ck_sim_sessions_step"),
        CheckConstraint(FAILED_STEP_SQL, name="ck_sim_sessions_failed_step"),
        CheckConstraint("commit_sha ~ '^[0-9a-f]{40}$'", name="ck_sim_sessions_commit_sha"),
        UniqueConstraint("instance_name", name="uq_sim_sessions_instance_name"),
        Index("ix_sim_sessions_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(Text)
    # Latest timeline step; see timeline.record_step.
    step: Mapped[str] = mapped_column(String(32))
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
    failed_step: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    provisioning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outputs_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["SessionEvent"]] = relationship(
        back_populates="session", order_by="SessionEvent.id", passive_deletes=True
    )
    log_excerpts: Mapped[list["SessionLogExcerpt"]] = relationship(
        back_populates="session", passive_deletes=True
    )
    alerts: Mapped[list["SessionAlert"]] = relationship(
        back_populates="session", passive_deletes=True
    )


class SessionEvent(Base):
    """One timeline step. Append-only, at most once per step, written under the session row lock."""

    __tablename__ = "session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "step", name=UQ_SESSION_EVENT),
        CheckConstraint(STEP_SQL, name="ck_session_events_step"),
        CheckConstraint(FAILED_STEP_SQL, name="ck_session_events_failed_step"),
        Index("ix_session_events_session_occurred", "session_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sim_sessions.id", ondelete="CASCADE"))
    step: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    brev_instance_id: Mapped[str | None] = mapped_column(String(128))
    git_commit: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    failed_step: Mapped[str | None] = mapped_column(String(32))

    session: Mapped[SimSession] = relationship(back_populates="events")


class SessionLogExcerpt(Base):
    """Capped log text for one source. A later post for the same source replaces the body.

    ``recording`` is the excerpt kept before the Brev instance is deleted.
    """

    __tablename__ = "session_log_excerpts"
    __table_args__ = (
        UniqueConstraint("session_id", "source", name=UQ_LOG_EXCERPT),
        CheckConstraint(LOG_SOURCE_SQL, name="ck_session_log_excerpts_source"),
        CheckConstraint("line_count >= 0", name="ck_session_log_excerpts_line_count"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sim_sessions.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    line_count: Mapped[int] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    session: Mapped[SimSession] = relationship(back_populates="log_excerpts")


class SessionAlert(Base):
    """One row per sent alert so a retry cannot post the same Slack message twice."""

    __tablename__ = "session_alerts"
    __table_args__ = (
        UniqueConstraint("session_id", "kind", "step", name=UQ_SESSION_ALERT),
        CheckConstraint(ALERT_KIND_SQL, name="ck_session_alerts_kind"),
        CheckConstraint(STEP_SQL, name="ck_session_alerts_step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sim_sessions.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    step: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[SimSession] = relationship(back_populates="alerts")
