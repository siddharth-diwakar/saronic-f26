import logging
import os
import threading
import time
import uuid
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fakes import SHA, FakeBrev, FakeClock, InMemoryStore
from sqlalchemy import select
from sqlalchemy.orm import Session

from saronic_api import healthcheck, worker
from saronic_api.brev import BrevError
from saronic_api.config import Settings
from saronic_api.sessions.models import SimSession, instance_name_for, is_managed_instance_name
from saronic_api.sessions.service import apply_stop
from saronic_api.sessions.store import SqlSessionStore
from saronic_api.sessions.transitions import DELETE_REISSUE_AFTER, MISSING_GRACE, Timeouts
from saronic_api.worker import Worker, run, write_heartbeat

LAUNCHABLE = "env-test"


def make(store=None, brev=None, clock=None, timeouts=None):
    store = store or InMemoryStore()
    brev = brev or FakeBrev()
    clock = clock or FakeClock()
    worker = Worker(store, brev, launchable_id=LAUNCHABLE, clock=clock, timeouts=timeouts)
    return worker, store, brev, clock


def stop(store, record, clock):
    """What POST /stop does, against the fake store."""
    with store.locked(record.id) as locked:
        apply_stop(locked, clock())


# --- Lifecycle ---


def test_full_lifecycle_requested_to_stopped():
    w, store, brev, clock = make()
    record = store.add()

    w.tick()
    assert record.status == "provisioning"
    assert record.provisioning_started_at == clock.now
    assert brev.calls_named("create") == [
        ("create", record.instance_name, LAUNCHABLE, {"SARONIC_REF": SHA})
    ]
    assert record.brev_status == "DEPLOYING" and record.instance_id == f"id-{record.instance_name}"

    brev.set(record.instance_name, "RUNNING", "BUILDING")
    clock.advance(10)
    w.tick()
    assert record.status == "running" and record.running_at == clock.now
    assert record.brev_build_status == "BUILDING"

    clock.advance(10)
    stop(store, record, clock)
    assert record.status == "stopping"
    w.tick()
    assert brev.calls_named("delete") == [("delete", record.instance_name)]
    assert record.delete_requested_at == clock.now

    clock.advance(10)
    w.tick()
    assert record.status == "stopped"
    assert record.error_code is None and record.ended_at == clock.now
    assert record.brev_status is None
    assert len(brev.calls_named("create")) == 1
    assert record.steps == [
        "requested",
        "provisioning",
        "instance_up",
        "teardown_requested",
        "stopped",
    ]
    assert brev.calls_named("stop") == []
    assert record.outputs_saved_at is not None
    assert record.log_excerpts[0].source == "recording"


def test_create_error_goes_to_stopping_then_deletes_then_failed():
    w, store, brev, clock = make()
    brev.create_error = BrevError("brev create exited with 1: quota exceeded")
    record = store.add()

    w.tick()
    assert record.status == "stopping"
    assert record.error_code == "brev_create_failed"
    assert "quota exceeded" in record.error_message
    # Not listed, but it may still exist: delete anyway.
    assert brev.calls_named("delete") == [("delete", record.instance_name)]

    clock.advance(30)
    w.tick()
    assert record.status == "stopping"  # waiting out the grace period for a late listing

    clock.advance(MISSING_GRACE.total_seconds())
    w.tick()
    assert record.status == "failed" and record.ended_at == clock.now
    assert record.steps == ["requested", "provisioning", "failed", "teardown_requested", "stopped"]
    failed = record.events[2]
    assert failed.failed_step == "instance_up" and failed.error_code == "brev_create_failed"
    assert "quota exceeded" in failed.error_detail


def test_create_error_where_instance_was_created_anyway_is_deleted():
    w, store, brev, clock = make()
    brev.create_error = BrevError("brev create timed out after 120s")
    brev.create_error_but_created = True
    record = store.add()

    w.tick()
    assert record.status == "stopping"
    assert brev.calls_named("delete") == [("delete", record.instance_name)]
    assert record.instance_name not in brev.instances

    clock.advance(MISSING_GRACE.total_seconds())
    w.tick()
    assert record.status == "failed"


def test_unexpected_create_exception_still_ends_in_stopping():
    w, store, brev, _ = make()
    brev.create = Mock(side_effect=RuntimeError("bug"))
    record = store.add()
    w.tick()
    assert record.status == "stopping"
    assert record.error_code == "brev_create_failed"


def test_brev_failure_while_running_deletes_and_fails():
    w, store, brev, clock = make()
    record = store.add()
    w.tick()
    brev.set(record.instance_name, "RUNNING")
    w.tick()
    assert record.status == "running"

    brev.set(record.instance_name, "FAILURE", "COMPLETED", "UNHEALTHY")
    clock.advance(10)
    w.tick()
    assert record.status == "stopping"
    assert record.error_code == "brev_instance_failed"
    assert brev.calls_named("delete") == [("delete", record.instance_name)]

    clock.advance(10)
    w.tick()
    assert record.status == "failed"
    assert record.steps[-4:] == ["instance_up", "failed", "teardown_requested", "stopped"]
    assert record.failed_step == "container_built"


def test_missing_instance_after_grace_fails_without_new_create():
    w, store, brev, clock = make()
    record = store.add()
    w.tick()
    brev.instances.clear()  # e.g. deleted from the Brev console

    clock.advance(MISSING_GRACE.total_seconds() - 1)
    w.tick()
    assert record.status == "provisioning"
    clock.advance(1)
    w.tick()
    assert record.status == "failed"
    assert record.error_code == "brev_instance_missing"
    assert len(brev.calls_named("create")) == 1
    assert record.steps == ["requested", "provisioning", "failed", "teardown_requested", "stopped"]


# --- Races and idempotency ---


def test_stop_during_failed_create_is_not_overwritten():
    w, store, brev, clock = make()
    record = store.add()
    brev.on_create = lambda _name: stop(store, record, clock)
    brev.create_error = BrevError("network error")

    w.tick()
    # The conditional update saw `stopping` and left it alone: no error recorded.
    assert record.status == "stopping"
    assert record.error_code is None
    assert brev.calls_named("delete") == [("delete", record.instance_name)]

    clock.advance(MISSING_GRACE.total_seconds())
    w.tick()
    assert record.status == "stopped"
    assert record.steps == ["requested", "provisioning", "teardown_requested", "stopped"]


def test_stop_during_successful_create_never_reports_running():
    w, store, brev, clock = make()
    record = store.add()
    brev.on_create = lambda _name: stop(store, record, clock)

    w.tick()
    assert record.status == "stopping"
    assert brev.calls_named("delete") == [("delete", record.instance_name)]
    clock.advance(10)
    w.tick()
    assert record.status == "stopped"
    assert record.running_at is None


def test_retried_create_name_taken_continues_as_created():
    w, store, brev, _ = make()
    record = store.add()
    brev.set(record.instance_name, "DEPLOYING", "PENDING")  # created before a worker restart

    w.tick()
    assert record.status == "provisioning" and record.error_code is None
    brev.set(record.instance_name, "RUNNING")
    w.tick()
    assert record.status == "running"


def test_repeated_ticks_create_once_and_throttle_delete():
    w, store, brev, clock = make()
    brev.delete_is_immediate = False
    record = store.add()
    for _ in range(5):
        w.tick()
    assert len(brev.calls_named("create")) == 1

    stop(store, record, clock)
    w.tick()
    assert len(brev.calls_named("delete")) == 1
    assert brev.instances[record.instance_name].status == "DELETING"
    # Brev now reports DELETING: never reissue while it is deleting.
    for _ in range(3):
        clock.advance(DELETE_REISSUE_AFTER.total_seconds())
        w.tick()
    assert len(brev.calls_named("delete")) == 1

    # Delete got lost (instance back to RUNNING): reissue only after 60 s.
    brev.set(record.instance_name, "RUNNING")
    clock.advance(1)
    record.delete_requested_at = clock.now
    w.tick()
    assert len(brev.calls_named("delete")) == 1
    clock.advance(DELETE_REISSUE_AFTER.total_seconds())
    w.tick()
    assert len(brev.calls_named("delete")) == 2

    brev.finish_deletes()
    w.tick()
    assert record.status == "stopped"
    assert len(brev.calls_named("create")) == 1


def test_requested_rows_locked_elsewhere_are_skipped():
    w, store, brev, _ = make()
    held = store.add()
    free = store.add()
    store.held_locks.add(held.id)
    w.tick()
    assert held.status == "requested" and free.status == "provisioning"
    assert [c[1] for c in brev.calls_named("create")] == [free.instance_name]


def test_claims_at_most_five_per_tick():
    w, store, brev, _ = make()
    for _ in range(7):
        store.add()
    w.provision()
    assert len(brev.calls_named("create")) == 5


# --- Resilience ---


def test_ls_failure_changes_nothing():
    w, store, brev, clock = make()
    record = store.add()
    w.tick()
    brev.ls_error = BrevError("brev ls exited with 1: 502")
    brev.instances.clear()
    clock.advance(MISSING_GRACE.total_seconds() * 10)
    w.tick()
    assert record.status == "provisioning"  # not marked missing without a fresh listing


def test_one_bad_row_does_not_stop_others():
    w, store, brev, _ = make()
    bad = store.add()
    good = store.add()
    w.tick()
    brev.set(bad.instance_name)
    brev.set(good.instance_name)
    store.broken.add(bad.id)
    w.tick()
    assert good.status == "running"
    assert bad.status == "provisioning"


def test_delete_error_is_retried_later():
    w, store, brev, clock = make()
    record = store.add()
    w.tick()
    stop(store, record, clock)
    brev.delete_error = BrevError("brev delete exited with 1: 500")
    w.tick()
    assert record.status == "stopping" and len(brev.calls_named("delete")) == 1
    brev.delete_error = None
    clock.advance(DELETE_REISSUE_AFTER.total_seconds())
    w.tick()
    assert len(brev.calls_named("delete")) == 2
    w.tick()
    assert record.status == "stopped"


def test_terminal_sessions_are_left_alone():
    w, store, brev, _ = make()
    store.add(status="stopped")
    store.add(status="failed")
    w.tick()
    assert brev.calls == [("ls",)]
    assert brev.calls_named("delete") == []


def test_logs_carry_session_context(caplog):
    caplog.set_level(logging.INFO, logger="saronic_api.worker")
    w, store, _, _ = make()
    record = store.add()
    w.tick()
    created = next(r for r in caplog.records if "Creating Brev instance" in r.getMessage())
    assert created.session_id == str(record.id)
    assert created.instance_name == record.instance_name
    assert created.commit_sha == SHA


# --- Loop, heartbeat, entry point, healthcheck ---


def test_run_loop_survives_tick_errors_beats_and_stops():
    stop_event = threading.Event()
    ticks = []

    class Flaky:
        def tick(self):
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("boom")
            stop_event.set()

    beats = Mock()
    run(Flaky(), interval_s=0, beat=beats, stop=stop_event)
    assert len(ticks) == 2 and beats.call_count == 2


def test_worker_beats_after_each_brev_call():
    store = InMemoryStore()
    beat = Mock()
    w = Worker(store, FakeBrev(), launchable_id=LAUNCHABLE, clock=FakeClock(), beat=beat)
    store.add()
    w.tick()  # ls before create, create, then the monitor ls
    assert beat.call_count == 3


def test_main_refuses_without_brev_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@db/x")
    monkeypatch.delenv("BREV_API_KEY", raising=False)
    monkeypatch.setattr(worker, "Settings", lambda: Settings(_env_file=None))
    build = Mock()
    monkeypatch.setattr(worker, "build_engine", build)
    assert worker.main() == 2
    build.assert_not_called()


def worker_settings(tmp_path, **overrides):
    values = {
        "database_url": "postgresql://a:b@db/x",
        "process_role": "worker",
        "worker_heartbeat_path": str(tmp_path / "heartbeat"),
    }
    return Settings(_env_file=None, **{**values, **overrides})


def test_healthcheck_worker_uses_heartbeat(monkeypatch, tmp_path):
    settings = worker_settings(tmp_path, worker_poll_interval_s=10, brev_command_timeout_s=120)
    monkeypatch.setattr(healthcheck, "Settings", lambda: settings)
    monkeypatch.setattr(healthcheck, "build_engine", lambda _: Mock())
    check_db = Mock()
    monkeypatch.setattr(healthcheck, "check_database", check_db)
    urlopen = Mock(side_effect=AssertionError("worker must not probe HTTP"))
    monkeypatch.setattr(healthcheck, "urlopen", urlopen)

    assert healthcheck.main() == 1  # no heartbeat yet
    heartbeat = tmp_path / "heartbeat"
    write_heartbeat(heartbeat)
    assert healthcheck.main() == 0
    check_db.assert_called_once()

    stale = time.time() - (3 * 10 + 120) - 1
    os.utime(heartbeat, (stale, stale))
    assert healthcheck.main() == 1


def test_healthcheck_api_role_unchanged(monkeypatch, tmp_path):
    settings = worker_settings(tmp_path, process_role="api")
    monkeypatch.setattr(healthcheck, "Settings", lambda: settings)
    monkeypatch.setattr(healthcheck, "build_engine", lambda _: Mock())
    monkeypatch.setattr(healthcheck, "check_database", Mock())
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(healthcheck, "urlopen", urlopen)
    assert healthcheck.main() == 0
    urlopen.assert_called_once_with("http://127.0.0.1:8000/openapi.json", timeout=2)
    urlopen.side_effect = OSError("connection refused")
    assert healthcheck.main() == 1


# --- PostgreSQL (SqlSessionStore) ---


def insert(engine, status="requested"):
    session_id = uuid.uuid4()
    with Session(engine) as db:
        db.add(
            SimSession(
                id=session_id,
                status=status,
                step="requested",
                ref="main",
                commit_sha=SHA,
                instance_name=instance_name_for(session_id),
            )
        )
        db.commit()
    return session_id


def load(engine, session_id):
    with Session(engine) as db:
        return db.get(SimSession, session_id)


def steps(engine, session_id):
    with Session(engine) as db:
        return [event.step for event in db.get(SimSession, session_id).events]


def sql_stop(engine, session_id, clock):
    """The stop endpoint's transaction."""
    with Session(engine) as db:
        record = db.scalars(
            select(SimSession).where(SimSession.id == session_id).with_for_update()
        ).one()
        apply_stop(record, clock())
        db.commit()


def test_existing_instance_is_not_created_again():
    w, store, brev, _ = make()
    record = store.add()
    brev.set(record.instance_name, "DEPLOYING", "PENDING")
    w.tick()
    assert brev.calls_named("create") == []
    assert record.status == "provisioning" and record.error_code is None
    assert record.instance_name in brev.instances


def test_suffixed_duplicate_from_create_is_deleted():
    w, store, brev, _ = make()
    record = store.add()
    brev.mint_duplicate = True
    w.tick()
    extra = f"{record.instance_name}-1"
    assert record.instance_name != extra
    assert extra not in brev.instances
    assert record.instance_name in brev.instances
    assert ("delete", extra) in brev.calls


def test_startup_idle_and_max_duration_delete_the_instance():
    limits = Timeouts(
        startup=timedelta(seconds=30),
        idle=timedelta(seconds=60),
        max_duration=timedelta(seconds=120),
    )
    w, store, brev, clock = make(timeouts=limits)
    startup = store.add()
    w.tick()
    clock.advance(30)
    w.tick()
    assert startup.status == "stopping" and startup.error_code == "startup_timeout"
    assert ("delete", startup.instance_name) in brev.calls

    idle = store.add()
    w.tick()
    brev.set(idle.instance_name)
    w.tick()
    assert idle.status == "running"
    clock.advance(60)
    w.tick()
    assert idle.error_code == "idle_timeout"

    limited = store.add()
    w.tick()
    brev.set(limited.instance_name)
    w.tick()
    limited.last_activity_at = clock.now
    clock.advance(120)
    w.tick()
    assert limited.error_code == "session_max_duration"
    assert ("delete", limited.instance_name) in brev.calls


def test_abandoned_instances_are_deleted_after_restart():
    w, store, brev, _ = make()
    leftover = store.add(status="stopped")
    brev.set(leftover.instance_name, "STOPPED")
    brev.set("saronic-abcdef123456", "RUNNING")
    brev.set("saronic-abcdef123456-1", "RUNNING")
    brev.set("saronic-spike-keep", "RUNNING")
    owned = store.add()  # requested; its name must survive the sweep
    brev.set(owned.instance_name, "RUNNING")

    w.tick()
    deleted = {call[1] for call in brev.calls_named("delete")}
    assert leftover.instance_name in deleted
    assert "saronic-abcdef123456" in deleted
    assert "saronic-abcdef123456-1" in deleted
    assert "saronic-spike-keep" not in deleted
    assert owned.instance_name not in deleted
    assert is_managed_instance_name(owned.instance_name)
    assert not is_managed_instance_name("saronic-spike-keep")


def test_sql_lifecycle_and_create_error(db_engine):
    clock = FakeClock()
    w, _, brev, _ = make(SqlSessionStore(db_engine), clock=clock)
    ok = insert(db_engine)
    w.tick()
    assert load(db_engine, ok).status == "provisioning"
    brev.set(instance_name_for(ok))
    w.tick()
    running = load(db_engine, ok)
    assert running.status == "running" and running.brev_status == "RUNNING"
    assert running.instance_id == f"id-{instance_name_for(ok)}"

    sql_stop(db_engine, ok, clock)
    w.tick()
    clock.advance(10)
    w.tick()
    assert load(db_engine, ok).status == "stopped"
    assert steps(db_engine, ok) == [
        "requested",
        "provisioning",
        "instance_up",
        "teardown_requested",
        "stopped",
    ]

    brev.create_error = BrevError("boom")
    bad = insert(db_engine)
    w.tick()
    failed = load(db_engine, bad)
    assert failed.status == "stopping" and failed.error_code == "brev_create_failed"
    assert ("delete", instance_name_for(bad)) in brev.calls
    clock.advance(MISSING_GRACE.total_seconds())
    w.tick()
    assert load(db_engine, bad).status == "failed"
    assert steps(db_engine, bad) == [
        "requested",
        "provisioning",
        "failed",
        "teardown_requested",
        "stopped",
    ]


def test_sql_stop_racing_create_is_not_overwritten(db_engine):
    clock = FakeClock()
    w, _, brev, _ = make(SqlSessionStore(db_engine), clock=clock)
    session_id = insert(db_engine)
    # The claim committed before create runs, so the stop endpoint's FOR UPDATE succeeds.
    brev.on_create = lambda _name: sql_stop(db_engine, session_id, clock)
    brev.create_error = BrevError("network error")
    w.tick()
    record = load(db_engine, session_id)
    assert record.status == "stopping" and record.error_code is None


def test_sql_claim_skips_locked_rows(db_engine):
    store = SqlSessionStore(db_engine)
    held = insert(db_engine)
    free = insert(db_engine)
    with Session(db_engine) as other:
        other.scalars(select(SimSession).where(SimSession.id == held).with_for_update()).one()
        claimed = store.claim_requested(5, FakeClock()())
        other.rollback()
    assert [c.id for c in claimed] == [free]
    assert load(db_engine, held).status == "requested"
    assert store.claim_requested(5, FakeClock()())[0].id == held


def test_sql_locked_unknown_id_yields_none(db_engine):
    with SqlSessionStore(db_engine).locked(uuid.uuid4()) as record:
        assert record is None


@pytest.mark.parametrize("status", ["requested", "stopped", "failed"])
def test_sql_monitored_ids_excludes_unmonitored(db_engine, status):
    insert(db_engine, status)
    watched = insert(db_engine, "running")
    assert SqlSessionStore(db_engine).monitored_ids() == [watched]
