from datetime import UTC, datetime, timedelta

import pytest

from saronic_api.brev import BrevInstance
from saronic_api.sessions.status import SessionStatus as S
from saronic_api.sessions.transitions import (
    DELETE_REISSUE_AFTER,
    MISSING_GRACE,
    NO_CHANGE,
    Action,
    SessionSnapshot,
    Timeouts,
    decide,
    stop_target,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=1)


def session(
    status,
    *,
    has_error=False,
    seen=True,
    started=LONG_AGO,
    delete_requested=None,
    running_at=None,
    last_activity_at=None,
    outputs_saved=None,
):
    return SessionSnapshot(
        status,
        has_error,
        seen,
        started,
        delete_requested,
        running_at,
        last_activity_at,
        outputs_saved,
    )


def instance(status="RUNNING", build="COMPLETED", health="HEALTHY"):
    return BrevInstance("saronic-abc", "ws-1", status, build, health, "g6e")


# --- stop requests (endpoint) ---


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.REQUESTED, S.STOPPED),
        (S.PROVISIONING, S.STOPPING),
        (S.RUNNING, S.STOPPING),
        (S.STOPPING, None),
        (S.STOPPED, None),
        (S.FAILED, None),
    ],
)
def test_stop_target(current, target):
    assert stop_target(current) == target


# --- provisioning / running ---


@pytest.mark.parametrize("brev_status", ["DEPLOYING", "STARTING", "SOMETHING_NEW", ""])
def test_provisioning_waits_until_brev_running(brev_status):
    assert decide(session(S.PROVISIONING), instance(brev_status, "PENDING"), NOW) == NO_CHANGE


def test_provisioning_to_running_on_brev_running_even_while_building():
    action = decide(session(S.PROVISIONING), instance("RUNNING", "BUILDING"), NOW)
    assert action == Action(status=S.RUNNING)


def test_running_stays_running():
    assert decide(session(S.RUNNING), instance(), NOW) == NO_CHANGE


@pytest.mark.parametrize("current", [S.PROVISIONING, S.RUNNING])
@pytest.mark.parametrize(
    "bad",
    [instance("FAILURE", ""), instance("UNHEALTHY"), instance("DEPLOYING", "CREATE_FAILED")],
)
def test_brev_failure_sets_error_stops_and_deletes(current, bad):
    action = decide(session(current), bad, NOW)
    assert action.status == S.STOPPING
    assert action.error_code == "brev_instance_failed"
    assert bad.status in action.error_message
    assert action.delete


def test_brev_failure_while_already_deleting_does_not_reissue():
    action = decide(session(S.RUNNING), instance("DELETING", "CREATE_FAILED"), NOW)
    assert action.status == S.STOPPING and not action.delete


@pytest.mark.parametrize("current", [S.PROVISIONING, S.RUNNING])
def test_missing_instance_within_grace_waits(current):
    started = NOW - MISSING_GRACE + timedelta(seconds=1)
    assert decide(session(current, started=started), None, NOW) == NO_CHANGE


@pytest.mark.parametrize("current", [S.PROVISIONING, S.RUNNING])
def test_missing_instance_after_grace_fails_and_deletes_in_case_ls_omitted_it(current):
    action = decide(session(current, started=NOW - MISSING_GRACE), None, NOW)
    assert action.status == S.FAILED
    assert action.error_code == "brev_instance_missing"
    assert action.delete


# --- stopping ---


@pytest.mark.parametrize("brev_status", ["RUNNING", "FAILURE", "STOPPED", "UNKNOWN"])
def test_stopping_issues_first_delete(brev_status):
    assert decide(session(S.STOPPING), instance(brev_status), NOW) == Action(
        delete=True, save_outputs=True
    )


def test_stopping_reissues_delete_at_most_every_60s():
    recent = session(S.STOPPING, delete_requested=NOW - DELETE_REISSUE_AFTER + timedelta(seconds=1))
    assert decide(recent, instance(), NOW) == NO_CHANGE
    stale = session(S.STOPPING, delete_requested=NOW - DELETE_REISSUE_AFTER)
    assert decide(stale, instance(), NOW) == Action(delete=True, save_outputs=True)


def test_stopping_never_deletes_while_brev_is_deleting():
    assert decide(session(S.STOPPING), instance("DELETING"), NOW) == NO_CHANGE


def test_stopping_blind_delete_when_not_yet_listed():
    # e.g. create timed out but Brev created it anyway; deleting a missing one is a no-op.
    action = decide(session(S.STOPPING, seen=False, started=NOW), None, NOW)
    assert action == Action(delete=True, save_outputs=True)


def test_stopping_never_seen_waits_out_grace_after_blind_delete():
    started = NOW - MISSING_GRACE + timedelta(seconds=1)
    snap = session(S.STOPPING, seen=False, started=started, delete_requested=NOW)
    assert decide(snap, None, NOW) == NO_CHANGE
    later = NOW + timedelta(seconds=1)
    assert decide(snap, None, later) == Action(status=S.STOPPED)


def test_stopping_seen_and_gone_ends_without_waiting_for_grace():
    snap = session(S.STOPPING, started=NOW - timedelta(seconds=30), delete_requested=NOW)
    assert decide(snap, None, NOW) == Action(status=S.STOPPED)


def test_stopping_gone_ends_stopped():
    snap = session(S.STOPPING, delete_requested=NOW - timedelta(seconds=5))
    assert decide(snap, None, NOW) == Action(status=S.STOPPED)


def test_stopping_gone_with_error_ends_failed():
    snap = session(S.STOPPING, has_error=True, delete_requested=NOW - timedelta(seconds=5))
    assert decide(snap, None, NOW) == Action(status=S.FAILED)


# --- requested and terminal ---


@pytest.mark.parametrize("current", [S.REQUESTED, S.STOPPED, S.FAILED])
@pytest.mark.parametrize("inst", [None, instance(), instance("FAILURE")])
def test_requested_and_terminal_states_never_change(current, inst):
    assert decide(session(current), inst, NOW) == NO_CHANGE


# --- timeouts and termination ---

LIMITS = Timeouts(
    startup=timedelta(minutes=45),
    idle=timedelta(minutes=20),
    max_duration=timedelta(hours=2),
)


def test_stopped_vm_still_bills_until_brev_delete():
    """`brev stop` leaves the disk. The session ends only once ls no longer lists it."""
    action = decide(session(S.STOPPING), instance("STOPPED"), NOW)
    assert action.status is None
    assert action.delete and action.save_outputs


def test_saved_recording_is_not_requested_again():
    snap = session(S.STOPPING, outputs_saved=NOW)
    assert decide(snap, instance("STOPPED"), NOW) == Action(delete=True)


def test_startup_timeout_deletes_a_vm_that_never_became_running():
    started = NOW - LIMITS.startup
    snap = session(S.PROVISIONING, started=started)
    action = decide(snap, instance("DEPLOYING", "BUILDING"), NOW, LIMITS)
    assert action.status == S.STOPPING
    assert action.error_code == "startup_timeout"
    assert action.delete and action.save_outputs


def test_idle_timeout_deletes_a_quiet_running_session():
    snap = session(
        S.RUNNING,
        running_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - LIMITS.idle,
    )
    action = decide(snap, instance(), NOW, LIMITS)
    assert action.error_code == "idle_timeout"
    assert action.delete


def test_max_duration_wins_over_a_session_that_is_still_active():
    snap = session(
        S.RUNNING,
        running_at=NOW - LIMITS.max_duration,
        last_activity_at=NOW,
    )
    action = decide(snap, instance(), NOW, LIMITS)
    assert action.error_code == "session_max_duration"


def test_timeouts_are_ignored_unless_the_worker_passes_them():
    snap = session(S.PROVISIONING, started=NOW - timedelta(hours=5))
    assert decide(snap, instance("DEPLOYING"), NOW) == NO_CHANGE
