"""Request and response bodies for the session timeline."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from saronic_api.models import LogSource, SessionStep, SimulationSession


class SessionCreate(BaseModel):
    git_commit: str = Field(min_length=1, max_length=64)
    brev_instance_id: str | None = Field(default=None, max_length=128)

    @field_validator("git_commit")
    @classmethod
    def strip_commit(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("git_commit is required")
        return value

    @field_validator("brev_instance_id")
    @classmethod
    def blank_instance(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SessionEventCreate(BaseModel):
    step: SessionStep
    brev_instance_id: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=64)
    error_detail: str | None = Field(default=None, max_length=2000)
    failed_step: SessionStep | None = None
    occurred_at: datetime | None = None
    log_tail: str | None = Field(default=None, max_length=1_000_000)

    @field_validator("brev_instance_id")
    @classmethod
    def blank_instance(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class SessionLogCreate(BaseModel):
    source: LogSource
    body: str = Field(max_length=1_000_000)
    attempt: int | None = Field(default=None, ge=1)


class SessionAttemptCreate(BaseModel):
    git_commit: str = Field(min_length=1, max_length=64)

    @field_validator("git_commit")
    @classmethod
    def strip_commit(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("git_commit is required")
        return value


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: SessionStep
    occurred_at: datetime
    attempt: int
    brev_instance_id: str | None
    git_commit: str
    error_code: str | None
    error_detail: str | None
    failed_step: SessionStep | None


class SessionOut(BaseModel):
    id: uuid.UUID
    status: SessionStep
    attempt: int
    brev_instance_id: str | None
    git_commit: str
    failed_step: SessionStep | None
    error_code: str | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    timeline: list[TimelineEventOut]
    logs: dict[str, str]


def to_session_out(row: SimulationSession) -> SessionOut:
    logs = {
        excerpt.source: excerpt.body
        for excerpt in row.log_excerpts
        if excerpt.attempt == row.attempt
    }
    timeline = sorted(row.events, key=lambda event: (event.occurred_at, event.id))
    return SessionOut(
        id=row.id,
        status=SessionStep(row.status),
        attempt=row.attempt,
        brev_instance_id=row.brev_instance_id,
        git_commit=row.git_commit,
        failed_step=SessionStep(row.failed_step) if row.failed_step is not None else None,
        error_code=row.error_code,
        error_detail=row.error_detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
        timeline=[TimelineEventOut.model_validate(event) for event in timeline],
        logs=logs,
    )
