import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from saronic_api.sessions.models import LogSource, SimSession
from saronic_api.sessions.status import SessionStatus, SessionStep


class CreateSessionRequest(BaseModel):
    """Body for `POST /api/v1/sessions`. Omit `ref` to use `SARONIC_DEFAULT_REF`."""

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

    step: SessionStep = Field(description="Next startup step, or `failed`.")
    error_code: str | None = Field(
        default=None, max_length=64, description="Required when step is failed."
    )
    error_detail: str | None = Field(
        default=None, max_length=2000, description="Short failure message. Not a log dump."
    )
    failed_step: SessionStep | None = Field(
        default=None, description="Pipeline step that failed. Required when step is failed."
    )
    occurred_at: datetime | None = Field(
        default=None, description="When the step happened. Must include a timezone."
    )
    log_tail: str | None = Field(
        default=None,
        max_length=1_000_000,
        description="Last log lines stored as failure_tail. Only accepted with step failed.",
    )

    @field_validator("occurred_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class SessionLogCreate(BaseModel):
    """Body for `POST /api/v1/sessions/{id}/logs`. The stored text is capped at 200 lines or 64 KB."""

    model_config = ConfigDict(extra="forbid")

    source: LogSource = Field(
        description="bootstrap, docker_build, isaac_launch, failure_tail, or recording."
    )
    body: str = Field(max_length=1_000_000, description="Raw log text. The tail is what is stored.")


class TimelineEvent(BaseModel):
    """One recorded step, in the order it was stored."""

    model_config = ConfigDict(from_attributes=True)

    step: SessionStep
    occurred_at: datetime
    brev_instance_id: str | None
    git_commit: str
    error_code: str | None
    error_detail: str | None
    failed_step: SessionStep | None


class BrevState(BaseModel):
    """Last Brev poll for this session. Unknown status strings are passed through unchanged."""

    instance_name: str
    instance_id: str | None
    # Raw Brev values from the last poll. `running` only means the VM is up; use these
    # to tell more. Unknown Brev strings are passed through unchanged.
    status: str | None
    build_status: str | None
    health_status: str | None
    checked_at: datetime | None


class SessionError(BaseModel):
    """Set when the session has failed. `failed_step` is the pipeline step that did not finish."""

    code: str
    message: str | None
    failed_step: SessionStep | None


class SessionResponse(BaseModel):
    """Session status, Brev poll, timeline, and the latest log excerpt per source."""

    id: uuid.UUID
    status: SessionStatus = Field(description="Worker state: requested through stopped, or failed.")
    # Latest timeline step: how far startup got, beyond what `status` says.
    step: SessionStep = Field(description="Latest timeline step, which can be ahead of status.")
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
    logs: dict[LogSource, str] = Field(
        description="Latest capped excerpt per source, including recording when one was stored."
    )

    @classmethod
    def from_record(cls, record: SimSession) -> "SessionResponse":
        """Build the response from a session row. Events and log excerpts must already be loaded."""
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
