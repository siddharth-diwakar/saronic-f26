"""Apply state-machine decisions to a session record.

Records are SimSession rows (or any object with the same attributes, as in the tests).
Callers hold the row lock and commit; nothing here calls Brev or the database.
"""

import uuid
from datetime import datetime
from typing import Protocol

from saronic_api.brev import BrevInstance
from saronic_api.sessions.status import SessionStatus
from saronic_api.sessions.transitions import NO_CHANGE, Action, SessionSnapshot, decide, stop_target

ERROR_MESSAGE_MAX = 2000


class SessionRecord(Protocol):
    id: uuid.UUID
    status: str
    instance_name: str
    commit_sha: str
    instance_id: str | None
    brev_status: str | None
    brev_build_status: str | None
    brev_health_status: str | None
    brev_checked_at: datetime | None
    error_code: str | None
    error_message: str | None
    provisioning_started_at: datetime | None
    running_at: datetime | None
    stop_requested_at: datetime | None
    delete_requested_at: datetime | None
    ended_at: datetime | None


def snapshot(record: SessionRecord) -> SessionSnapshot:
    return SessionSnapshot(
        status=SessionStatus(record.status),
        has_error=record.error_code is not None,
        provisioning_started_at=record.provisioning_started_at,
        delete_requested_at=record.delete_requested_at,
    )


def apply_stop(record: SessionRecord, now: datetime) -> bool:
    target = stop_target(SessionStatus(record.status))
    if target is None:
        return False
    if target == SessionStatus.STOPPED:
        record.ended_at = now
    else:
        record.stop_requested_at = now
    record.status = target.value
    return True


def apply_poll(record: SessionRecord, instance: BrevInstance | None, now: datetime) -> Action:
    """Record this tick's Brev view of the instance, then apply decide()."""
    record.brev_checked_at = now
    if instance is None:
        record.brev_status = record.brev_build_status = record.brev_health_status = None
    else:
        record.instance_id = instance.id or record.instance_id
        record.brev_status = instance.status
        record.brev_build_status = instance.build_status
        record.brev_health_status = instance.health_status
    action = decide(snapshot(record), instance, now)
    apply_action(record, action, now)
    return action


def apply_action(record: SessionRecord, action: Action, now: datetime) -> None:
    if action == NO_CHANGE:
        return
    if action.error_code and record.error_code is None:
        set_error(record, action.error_code, action.error_message or "")
    if action.status is not None:
        record.status = action.status.value
        if action.status == SessionStatus.RUNNING:
            record.running_at = now
        elif action.status in (SessionStatus.STOPPED, SessionStatus.FAILED):
            record.ended_at = now
    if action.delete:
        record.delete_requested_at = now


def apply_create_failure(record: SessionRecord, message: str) -> bool:
    """A create error may still have created the instance, so stop (and delete) it.

    Only applies while provisioning, so a concurrent stop is never overwritten.
    """
    if record.status != SessionStatus.PROVISIONING:
        return False
    set_error(record, "brev_create_failed", message)
    record.status = SessionStatus.STOPPING.value
    return True


def set_error(record: SessionRecord, code: str, message: str) -> None:
    record.error_code = code
    record.error_message = message[:ERROR_MESSAGE_MAX]
