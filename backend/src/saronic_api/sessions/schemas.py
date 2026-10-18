import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from saronic_api.sessions.models import SimSession
from saronic_api.sessions.status import SessionStatus


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str | None = Field(
        default=None,
        max_length=255,
        description="Branch, tag, refs/heads/<x>, refs/tags/<x> or full SHA. "
        "Defaults to SARONIC_DEFAULT_REF.",
    )


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


class SessionResponse(BaseModel):
    id: uuid.UUID
    status: SessionStatus
    ref: str
    commit_sha: str
    brev: BrevState
    error: SessionError | None
    created_at: datetime
    updated_at: datetime
    running_at: datetime | None
    stop_requested_at: datetime | None
    ended_at: datetime | None

    @classmethod
    def from_record(cls, record: SimSession) -> "SessionResponse":
        return cls(
            id=record.id,
            status=SessionStatus(record.status),
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
                SessionError(code=record.error_code, message=record.error_message)
                if record.error_code
                else None
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
            running_at=record.running_at,
            stop_requested_at=record.stop_requested_at,
            ended_at=record.ended_at,
        )
