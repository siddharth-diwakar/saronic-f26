import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from saronic_api.sessions.models import LogSource, SimSession
from saronic_api.sessions.status import SessionStatus, SessionStep


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str | None = Field(
        default=None,
        max_length=255,
        description="Branch, tag, refs/heads/<x>, refs/tags/<x> or full SHA. "
        "Defaults to SARONIC_DEFAULT_REF.",
    )


class SessionEventCreate(BaseModel):
    """A step reported by the instance: container_built through ready, or failed."""

    model_config = ConfigDict(extra="forbid")

    step: SessionStep
    error_code: str | None = Field(default=None, max_length=64)
    error_detail: str | None = Field(default=None, max_length=2000)
    failed_step: SessionStep | None = None
    occurred_at: datetime | None = None
    log_tail: str | None = Field(default=None, max_length=1_000_000)

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class SessionLogCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: LogSource
    body: str = Field(max_length=1_000_000)


class TimelineEvent(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    step: SessionStep
    occurred_at: datetime
    brev_instance_id: str | None
    git_commit: str
    error_code: str | None
    error_detail: str | None
    failed_step: SessionStep | None


class BrevState(BaseModel):
    instance_name: str
    instance_id: str | None
    # Raw Brev values from the last poll. `running` only means the VM is up; use these
    # to tell more. Unknown Brev strings are passed through unchanged.
    status: str | None
    build_status: str | None
    health_status: str | None
    checked_at: datetime | None


class SessionError(BaseModel):
    code: str
    message: str | None
    failed_step: SessionStep | None


class SessionResponse(BaseModel):
    id: uuid.UUID
    status: SessionStatus
    # Latest timeline step: how far startup got, beyond what `status` says.
    step: SessionStep
    ref: str
    commit_sha: str
    brev: BrevState
    error: SessionError | None
    created_at: datetime
    updated_at: datetime
    running_at: datetime | None
    stop_requested_at: datetime | None
    ended_at: datetime | None
    timeline: list[TimelineEvent]
    # Latest capped excerpt per source (bootstrap, docker_build, isaac_launch, failure_tail).
    logs: dict[LogSource, str]

    @classmethod
    def from_record(cls, record: SimSession) -> "SessionResponse":
        return cls(
            id=record.id,
            status=SessionStatus(record.status),
            step=SessionStep(record.step),
            ref=record.ref,
            commit_sha=record.commit_sha,
            brev=BrevState(
                instance_name=record.instance_name,
                instance_id=record.instance_id,
                status=record.brev_status,
                build_status=record.brev_build_status,
                health_status=record.brev_health_status,
                checked_at=record.brev_checked_at,
            ),
            error=(
                SessionError(
                    code=record.error_code,
                    message=record.error_message,
                    failed_step=record.failed_step,
                )
                if record.error_code
                else None
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
            running_at=record.running_at,
            stop_requested_at=record.stop_requested_at,
            ended_at=record.ended_at,
            timeline=[TimelineEvent.model_validate(event) for event in record.events],
            logs={LogSource(excerpt.source): excerpt.body for excerpt in record.log_excerpts},
        )
