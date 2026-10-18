"""Session timeline tables. Events are append-only; teardown is a later event."""

import enum
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
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
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saronic_api.database import Base

UQ_SESSION_EVENT = "uq_session_events_session_attempt_step"
UQ_LOG_EXCERPT = "uq_session_log_excerpts_session_attempt_source"
UQ_SESSION_ALERT = "uq_session_alerts_session_attempt_kind_step"


class SessionStep(enum.StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    INSTANCE_UP = "instance_up"
    CONTAINER_BUILT = "container_built"
    SCENE_LOADED = "scene_loaded"
    PHYSICS_RUNNING = "physics_running"
    CAMERAS_READY = "cameras_ready"
    READY = "ready"
    FAILED = "failed"
    TEARDOWN_REQUESTED = "teardown_requested"
    STOPPED = "stopped"


class LogSource(enum.StrEnum):
    BOOTSTRAP = "bootstrap"
    DOCKER_BUILD = "docker_build"
    ISAAC_LAUNCH = "isaac_launch"
    FAILURE_TAIL = "failure_tail"


class AlertKind(enum.StrEnum):
    FAILED = "failed"
    STUCK = "stuck"


FORWARD_STEPS: tuple[SessionStep, ...] = (
    SessionStep.REQUESTED,
    SessionStep.PROVISIONING,
    SessionStep.INSTANCE_UP,
    SessionStep.CONTAINER_BUILT,
    SessionStep.SCENE_LOADED,
    SessionStep.PHYSICS_RUNNING,
    SessionStep.CAMERAS_READY,
    SessionStep.READY,
)
# Ready is a successful terminal step. Failure is still possible through cameras_ready.
IN_PROGRESS_STEPS: frozenset[SessionStep] = frozenset(FORWARD_STEPS) - {SessionStep.READY}


def _sql_list(steps: Iterable[enum.StrEnum]) -> str:
    return ", ".join(f"'{step.value}'" for step in steps)


STATUS_SQL = f"status IN ({_sql_list(tuple(SessionStep))})"
STEP_SQL = f"step IN ({_sql_list(tuple(SessionStep))})"
FAILED_STEP_SQL = f"failed_step IS NULL OR failed_step IN ({_sql_list(FORWARD_STEPS)})"
LOG_SOURCE_SQL = f"source IN ({_sql_list(tuple(LogSource))})"
ALERT_KIND_SQL = f"kind IN ({_sql_list(tuple(AlertKind))})"


class SimulationSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(STATUS_SQL, name="ck_sessions_status"),
        CheckConstraint(FAILED_STEP_SQL, name="ck_sessions_failed_step"),
        CheckConstraint("attempt >= 1", name="ck_sessions_attempt"),
        Index("ix_sessions_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    brev_instance_id: Mapped[str | None] = mapped_column(String(128))
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    failed_step: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    events: Mapped[list["SessionEvent"]] = relationship(
        back_populates="session",
        order_by="SessionEvent.occurred_at, SessionEvent.id",
    )
    log_excerpts: Mapped[list["SessionLogExcerpt"]] = relationship(back_populates="session")
    alerts: Mapped[list["SessionAlert"]] = relationship(back_populates="session")


class SessionEvent(Base):
    __tablename__ = "session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "attempt", "step", name=UQ_SESSION_EVENT),
        CheckConstraint(STEP_SQL, name="ck_session_events_step"),
        CheckConstraint(FAILED_STEP_SQL, name="ck_session_events_failed_step"),
        CheckConstraint("attempt >= 1", name="ck_session_events_attempt"),
        Index("ix_session_events_session_occurred", "session_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    brev_instance_id: Mapped[str | None] = mapped_column(String(128))
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    failed_step: Mapped[str | None] = mapped_column(String(32))

    session: Mapped[SimulationSession] = relationship(back_populates="events")


class SessionLogExcerpt(Base):
    __tablename__ = "session_log_excerpts"
    __table_args__ = (
        UniqueConstraint("session_id", "attempt", "source", name=UQ_LOG_EXCERPT),
        CheckConstraint(LOG_SOURCE_SQL, name="ck_session_log_excerpts_source"),
        CheckConstraint("attempt >= 1", name="ck_session_log_excerpts_attempt"),
        CheckConstraint("line_count >= 0", name="ck_session_log_excerpts_line_count"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[SimulationSession] = relationship(back_populates="log_excerpts")


class SessionAlert(Base):
    """One row per sent alert so a retry cannot post the same Slack message twice."""

    __tablename__ = "session_alerts"
    __table_args__ = (
        UniqueConstraint("session_id", "attempt", "kind", "step", name=UQ_SESSION_ALERT),
        CheckConstraint(ALERT_KIND_SQL, name="ck_session_alerts_kind"),
        CheckConstraint(STEP_SQL, name="ck_session_alerts_step"),
        CheckConstraint("attempt >= 1", name="ck_session_alerts_attempt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    session: Mapped[SimulationSession] = relationship(back_populates="alerts")
