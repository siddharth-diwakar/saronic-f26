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
class SessionSnapshot:
    status: SessionStatus
    has_error: bool
    instance_seen: bool  # listed by `brev ls` at least once
    provisioning_started_at: datetime | None
    delete_requested_at: datetime | None


@dataclass(frozen=True)
class Action:
    status: SessionStatus | None = None  # None: unchanged
    error_code: str | None = None
    error_message: str | None = None
    delete: bool = False  # issue `brev delete` (after committing, outside the row lock)


NO_CHANGE = Action()


def decide(session: SessionSnapshot, instance: BrevInstance | None, now: datetime) -> Action:
    """What to do with a monitored session given the instance from this tick's `brev ls`."""
    if session.status in (SessionStatus.PROVISIONING, SessionStatus.RUNNING):
        if instance is None:
            if _within_grace(session, now):
                return NO_CHANGE
            # Delete anyway: if `ls` merely omitted a live instance, this stops the GPU.
            return Action(
                status=SessionStatus.FAILED,
                error_code="brev_instance_missing",
                error_message="The Brev instance is no longer listed by Brev.",
                delete=True,
            )
        if _instance_failed(instance):
            return Action(
                status=SessionStatus.STOPPING,
                error_code="brev_instance_failed",
                error_message=(
                    f"Brev reported status={instance.status!r} "
                    f"build_status={instance.build_status!r} "
                    f"health_status={instance.health_status!r}."
                ),
                delete=_should_delete(session, instance, now),
            )
        if session.status == SessionStatus.PROVISIONING and instance.status == BREV_RUNNING:
            return Action(status=SessionStatus.RUNNING)
        return NO_CHANGE

    if session.status == SessionStatus.STOPPING:
        if instance is None:
            if session.delete_requested_at is None:
                # Not listed yet does not mean never created (e.g. a create that timed out).
                # Deleting a missing instance is a no-op success.
                return Action(delete=True)
            # Seen before and now gone: deleted. Never seen: wait in case `ls` is lagging.
            if not session.instance_seen and _within_grace(session, now):
                return NO_CHANGE
            return Action(
                status=SessionStatus.FAILED if session.has_error else SessionStatus.STOPPED
            )
        if _should_delete(session, instance, now):
            return Action(delete=True)
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
