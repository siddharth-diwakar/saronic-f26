"""Session state machine (PRD §6.4) as pure functions: no DB, no Brev, no clock.

Invariant: every path that may have created an instance ends in a delete. A failed
session must never leave a GPU running.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from saronic_api.brev import BrevInstance
from saronic_api.sessions.status import SessionStatus

# `ls` may not list an instance immediately after create.
MISSING_GRACE = timedelta(minutes=2)
DELETE_REISSUE_AFTER = timedelta(seconds=60)

BREV_FAILED_STATUSES = frozenset({"FAILURE", "UNHEALTHY"})
BREV_FAILED_BUILD_STATUSES = frozenset({"CREATE_FAILED"})
BREV_DELETING = "DELETING"
BREV_RUNNING = "RUNNING"


@dataclass(frozen=True)
class Timeouts:
    """How long a session may provision, sit idle, or stay running before it is deleted."""

    startup: timedelta = timedelta(minutes=45)
    idle: timedelta = timedelta(minutes=20)
    max_duration: timedelta = timedelta(hours=2)


@dataclass(frozen=True)
class SessionSnapshot:
    """The session fields `decide` reads. Callers hold the row lock."""

    status: SessionStatus
    has_error: bool
    instance_seen: bool  # listed by `brev ls` at least once
    provisioning_started_at: datetime | None
    delete_requested_at: datetime | None
    running_at: datetime | None = None
    last_activity_at: datetime | None = None
    # Set once a recording log has been stored. Delete waits for that.
    outputs_saved_at: datetime | None = None


@dataclass(frozen=True)
class Action:
    """What the worker should do after one poll. `delete` runs only after the row commits."""

    status: SessionStatus | None = None  # None: unchanged
    error_code: str | None = None
    error_message: str | None = None
    delete: bool = False  # issue `brev delete` (after committing, outside the row lock)
    # Store a recording log in the same transaction, before that delete runs.
    save_outputs: bool = False


NO_CHANGE = Action()


def decide(
    session: SessionSnapshot,
    instance: BrevInstance | None,
    now: datetime,
    timeouts: Timeouts | None = None,
) -> Action:
    """What to do with a monitored session given the instance from this tick's `brev ls`.

    A session is terminated only when `brev ls` no longer lists the instance. `STOPPED`
    still bills for the disk, so it is not terminated and still needs `brev delete`.
    """
    if session.status in (SessionStatus.PROVISIONING, SessionStatus.RUNNING):
        if instance is not None and _instance_failed(instance):
            return _maybe_delete(
                session,
                instance,
                now,
                status=SessionStatus.STOPPING,
                error_code="brev_instance_failed",
                error_message=(
                    f"Brev reported status={instance.status!r} "
                    f"build_status={instance.build_status!r} "
                    f"health_status={instance.health_status!r}."
                ),
            )
        timed_out = _timeout_action(session, now, timeouts)
        if timed_out is not None:
            return timed_out
        if instance is None:
            if _within_grace(session, now):
                return NO_CHANGE
            # Delete anyway: if `ls` merely omitted a live instance, this stops the GPU.
            return _deleting(
                session,
                status=SessionStatus.FAILED,
                error_code="brev_instance_missing",
                error_message="The Brev instance is no longer listed by Brev.",
            )
        if session.status == SessionStatus.PROVISIONING and instance.status == BREV_RUNNING:
            return Action(status=SessionStatus.RUNNING)
        return NO_CHANGE

    if session.status == SessionStatus.STOPPING:
        if instance is None:
            if session.delete_requested_at is None:
                # Not listed yet does not mean never created (e.g. a create that timed out).
                # Deleting a missing instance is a no-op success.
                return _deleting(session)
            # Seen before and now gone: deleted. Never seen: wait in case `ls` is lagging.
            if not session.instance_seen and _within_grace(session, now):
                return NO_CHANGE
            return Action(
                status=SessionStatus.FAILED if session.has_error else SessionStatus.STOPPED
            )
        # Still listed, including STOPPED: the VM halt did not end GPU/storage billing.
        if _should_delete(session, instance, now):
            return _deleting(session)
        return NO_CHANGE

    # requested is handled by the provisioning step; stopped/failed are terminal.
    return NO_CHANGE


def stop_target(status: SessionStatus) -> SessionStatus | None:
    """Status after a stop request, or None when the request changes nothing."""
    if status == SessionStatus.REQUESTED:
        return SessionStatus.STOPPED  # cancelled; no instance exists yet
    if status in (SessionStatus.PROVISIONING, SessionStatus.RUNNING):
        return SessionStatus.STOPPING
    return None  # stopping/stopped/failed: idempotent


def _instance_failed(instance: BrevInstance) -> bool:
    return (
        instance.status in BREV_FAILED_STATUSES
        or instance.build_status in BREV_FAILED_BUILD_STATUSES
    )


def _deleting(session: SessionSnapshot, **kwargs) -> Action:
    return Action(delete=True, save_outputs=session.outputs_saved_at is None, **kwargs)


def _maybe_delete(
    session: SessionSnapshot, instance: BrevInstance, now: datetime, **kwargs
) -> Action:
    if _should_delete(session, instance, now):
        return _deleting(session, **kwargs)
    return Action(**kwargs)


def _timeout_action(
    session: SessionSnapshot, now: datetime, timeouts: Timeouts | None
) -> Action | None:
    if timeouts is None:
        return None
    if session.status == SessionStatus.PROVISIONING and _exceeded(
        session.provisioning_started_at, timeouts.startup, now
    ):
        return _deleting(
            session,
            status=SessionStatus.STOPPING,
            error_code="startup_timeout",
            error_message=f"Instance was not running within {timeouts.startup}.",
        )
    if session.status != SessionStatus.RUNNING:
        return None
    if _exceeded(session.running_at, timeouts.max_duration, now):
        return _deleting(
            session,
            status=SessionStatus.STOPPING,
            error_code="session_max_duration",
            error_message=f"Session exceeded the maximum duration of {timeouts.max_duration}.",
        )
    activity = session.last_activity_at or session.running_at
    if _exceeded(activity, timeouts.idle, now):
        return _deleting(
            session,
            status=SessionStatus.STOPPING,
            error_code="idle_timeout",
            error_message=f"No session activity for {timeouts.idle}.",
        )
    return None


def _exceeded(started: datetime | None, limit: timedelta, now: datetime) -> bool:
    return started is not None and now - started >= limit


def _should_delete(session: SessionSnapshot, instance: BrevInstance, now: datetime) -> bool:
    if instance.status == BREV_DELETING:
        return False
    return (
        session.delete_requested_at is None
        or now - session.delete_requested_at >= DELETE_REISSUE_AFTER
    )


def _within_grace(session: SessionSnapshot, now: datetime) -> bool:
    started = session.provisioning_started_at
    return started is not None and now - started < MISSING_GRACE
