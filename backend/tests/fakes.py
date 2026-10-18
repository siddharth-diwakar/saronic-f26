"""Minimal fakes for worker tests. Replace with Tanush's shared fake BrevClient when it lands."""

import copy
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from saronic_api.brev import BrevError, BrevInstance, BrevNameTaken
from saronic_api.sessions.models import instance_name_for
from saronic_api.sessions.store import ClaimedSession

SHA = "c" * 40
START = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeBrev:
    """In-memory BrevClient. Instances appear on create and vanish on delete."""

    def __init__(self) -> None:
        self.instances: dict[str, BrevInstance] = {}
        self.calls: list[tuple] = []
        self.create_error: BrevError | None = None
        self.create_error_but_created = False  # e.g. CLI timed out after Brev accepted
        self.on_create: Callable[[str], None] | None = None
        self.ls_error: BrevError | None = None
        self.delete_error: BrevError | None = None
        self.delete_is_immediate = True  # else the instance shows DELETING until finish_deletes()

    def create(self, *, name: str, launchable_id: str, params: dict[str, str]) -> str:
        self.calls.append(("create", name, launchable_id, dict(params)))
        if self.on_create:
            self.on_create(name)
        if name in self.instances:
            raise BrevNameTaken(f"workspace '{name}' already exists")
        if self.create_error:
            if self.create_error_but_created:
                self.set(name, "DEPLOYING", "PENDING")
            raise self.create_error
        self.set(name, "DEPLOYING", "PENDING")
        return name

    def set(self, name, status="RUNNING", build="COMPLETED", health="HEALTHY") -> None:
        self.instances[name] = BrevInstance(name, f"id-{name}", status, build, health, "g6e")

    def status(self, name: str) -> BrevInstance | None:
        return self.instances.get(name)

    def list_instances(self) -> list[BrevInstance]:
        self.calls.append(("ls",))
        if self.ls_error:
            raise self.ls_error
        return list(self.instances.values())

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    def delete(self, name: str) -> None:
        self.calls.append(("delete", name))
        if self.delete_error:
            raise self.delete_error
        if name not in self.instances:
            return  # not found counts as success
        if self.delete_is_immediate:
            del self.instances[name]
        else:
            self.set(name, "DELETING", self.instances[name].build_status)

    def finish_deletes(self) -> None:
        for name in [n for n, i in self.instances.items() if i.status == "DELETING"]:
            del self.instances[name]

    def calls_named(self, kind: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == kind]


@dataclass
class FakeRecord:
    id: uuid.UUID
    status: str
    instance_name: str
    commit_sha: str = SHA
    ref: str = "main"
    instance_id: str | None = None
    brev_status: str | None = None
    brev_build_status: str | None = None
    brev_health_status: str | None = None
    brev_checked_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    provisioning_started_at: datetime | None = None
    running_at: datetime | None = None
    stop_requested_at: datetime | None = None
    delete_requested_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: START)


class InMemoryStore:
    """SessionStore with the same claim/lock semantics as SqlSessionStore."""

    def __init__(self) -> None:
        self.records: dict[uuid.UUID, FakeRecord] = {}
        self.held_locks: set[uuid.UUID] = set()  # rows another transaction holds
        self.broken: set[uuid.UUID] = set()  # rows whose update raises

    def add(self, status: str = "requested", **fields) -> FakeRecord:
        session_id = uuid.uuid4()
        record = FakeRecord(session_id, status, instance_name_for(session_id), **fields)
        self.records[session_id] = record
        return record

    def claim_requested(self, limit: int, now: datetime) -> list[ClaimedSession]:
        claimed = []
        for record in self.records.values():
            if len(claimed) == limit:
                break
            if record.status == "requested" and record.id not in self.held_locks:
                record.status = "provisioning"
                record.provisioning_started_at = now
                claimed.append(ClaimedSession(record.id, record.instance_name, record.commit_sha))
        return claimed

    def monitored_ids(self) -> list[uuid.UUID]:
        return [
            r.id
            for r in self.records.values()
            if r.status in {"provisioning", "running", "stopping"}
        ]

    @contextmanager
    def locked(self, session_id: uuid.UUID):
        if session_id in self.broken:
            raise RuntimeError("simulated database error")
        record = self.records.get(session_id)
        backup = copy.deepcopy(record)
        try:
            yield record
        except BaseException:
            if record is not None:
                record.__dict__.update(backup.__dict__)  # roll back
            raise
