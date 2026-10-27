"""Background worker: the only process that calls Brev. Run with `python -m saronic_api.worker`.

Each tick:
1. Provision: claim requested rows (committed), then `brev create` outside any transaction.
   A name that already exists is not created again. A second name minted by a retry is deleted.
2. Monitor: one `brev ls` for every managed instance. Apply decide() per active row under a
   short row lock (timeouts, and a recording log before delete). Delete instances whose
   names we own but no active session does — leftovers after a restart.
3. Delete: issue the deletes decided in step 2, after those transactions committed.
   `brev stop` is never used: a stopped VM still bills for the disk. A session ends only
   once `brev ls` no longer lists the instance.

Instance names are derived from session IDs, so a worker killed mid-tick recovers on restart.
"""

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from saronic_api.brev import BrevClient, BrevError, BrevInstance, BrevNameTaken, CliBrevClient
from saronic_api.config import Settings
from saronic_api.database import build_engine, check_database
from saronic_api.sessions.models import is_managed_instance_name
from saronic_api.sessions.service import apply_create_failure, apply_poll
from saronic_api.sessions.status import MONITORED_STATUSES, TERMINAL_STATUSES
from saronic_api.sessions.store import ClaimedSession, SessionStore, SqlSessionStore
from saronic_api.sessions.transitions import Timeouts

logger = logging.getLogger(__name__)

CLAIM_BATCH = 5


def utcnow() -> datetime:
    """Current time in UTC. Tests replace the worker clock instead of this."""
    return datetime.now(UTC)


class Worker:
    def __init__(
        self,
        store: SessionStore,
        brev: BrevClient,
        *,
        launchable_id: str,
        clock: Callable[[], datetime] = utcnow,
        beat: Callable[[], None] = lambda: None,
        timeouts: Timeouts | None = None,
    ) -> None:
        self._store = store
        self._brev = brev
        self._launchable_id = launchable_id
        self._clock = clock
        self._timeouts = timeouts or Timeouts()
        # Called after each Brev call so a slow CLI call doesn't look like a hung worker.
        self._beat = beat

    def tick(self) -> None:
        """Provision requested sessions, poll Brev, reap abandoned instances, then delete."""
        self.provision()
        self.monitor()

    def provision(self) -> None:
        """Claim requested sessions and create each Brev instance, unless that name already exists."""
        claimed_rows = self._store.claim_requested(CLAIM_BATCH, self._clock())
        if not claimed_rows:
            return
        listed = self._listed_names()
        for claimed in claimed_rows:
            extra = _log_extra(claimed.id, claimed.instance_name, claimed.commit_sha)
            if listed is not None and claimed.instance_name in listed:
                logger.info("Instance already exists; not creating another", extra=extra)
                continue
            logger.info("Creating Brev instance %s", claimed.instance_name, extra=extra)
            try:
                name = self._brev.create(
                    name=claimed.instance_name,
                    launchable_id=self._launchable_id,
                    params={"SARONIC_REF": claimed.commit_sha},
                )
            except BrevNameTaken:
                # Same session retried (e.g. after a restart); the instance is ours.
                logger.info("Instance already exists; treating as created", extra=extra)
                continue
            # On any create error the instance may exist anyway, so the session goes to
            # stopping, which always ends in a delete.
            except BrevError as exc:
                logger.warning("brev create failed: %s", exc, extra=extra)
                failure = partial(apply_create_failure, message=str(exc), now=self._clock())
                self._update(claimed, failure)
                continue
            except Exception:
                logger.exception("Unexpected error from brev create", extra=extra)
                failure = partial(
                    apply_create_failure,
                    message="Unexpected error in brev create",
                    now=self._clock(),
                )
                self._update(claimed, failure)
                continue
            finally:
                self._beat()
            if name != claimed.instance_name:
                self._reject_duplicate_name(claimed, name, extra)

    def monitor(self) -> None:
        """Poll `brev ls` once, apply timeouts and deletes, and remove unmanaged leftover instances."""
        ids = self._store.monitored_ids()
        try:
            instances = {instance.name: instance for instance in self._brev.list_instances()}
        # Never decide anything (e.g. "missing") without a fresh listing.
        except BrevError as exc:
            logger.warning("brev ls failed; skipping monitor this tick: %s", exc)
            return
        except Exception:
            logger.exception("Unexpected error from brev ls; skipping monitor this tick")
            return
        finally:
            self._beat()

        self._reap_abandoned(instances)
        if not ids:
            return
        now = self._clock()
        deletes: list[ClaimedSession] = []
        for session_id in ids:
            try:
                with self._store.locked(session_id) as record:
                    if record is None or record.status not in MONITORED_STATUSES:
                        continue
                    before = record.status
                    action = apply_poll(
                        record, instances.get(record.instance_name), now, self._timeouts
                    )
                    extra = _log_extra(record.id, record.instance_name, record.commit_sha)
                    if record.status != before:
                        logger.info(
                            "Session %s: %s -> %s", record.id, before, record.status, extra=extra
                        )
                    if action.delete:
                        deletes.append(
                            ClaimedSession(record.id, record.instance_name, record.commit_sha)
                        )
            except Exception:
                logger.exception("Failed to update session %s", session_id)

        for target in deletes:
            extra = _log_extra(target.id, target.instance_name, target.commit_sha)
            logger.info("Deleting Brev instance %s", target.instance_name, extra=extra)
            try:
                self._brev.delete(target.instance_name)
            # delete_requested_at is already committed; retried after DELETE_REISSUE_AFTER.
            except BrevError as exc:
                logger.warning("brev delete failed: %s", exc, extra=extra)
            except Exception:
                logger.exception("Unexpected error from brev delete", extra=extra)
            finally:
                self._beat()

    def _listed_names(self) -> set[str] | None:
        try:
            return {instance.name for instance in self._brev.list_instances()}
        except BrevError as exc:
            logger.warning("brev ls failed before create; creating anyway: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected error from brev ls before create")
            return None
        finally:
            self._beat()

    def _reject_duplicate_name(
        self, claimed: ClaimedSession, name: str, extra: dict[str, str]
    ) -> None:
        """A create that returns a new name may have started a second GPU. Drop that one."""
        logger.warning("Brev created %s instead of the session name", name, extra=extra)
        current = self._listed_names()
        original_exists = current is not None and claimed.instance_name in current
        if original_exists:
            try:
                self._brev.delete(name)
            except BrevError as exc:
                logger.warning("Could not delete duplicate instance %s: %s", name, exc, extra=extra)
            except Exception:
                logger.exception(
                    "Unexpected error deleting duplicate instance %s", name, extra=extra
                )
            finally:
                self._beat()
            return
        # The only instance is the renamed one (Brev did not keep the requested name).
        logger.warning("Adopting Brev name %s", name, extra=extra)
        self._update(claimed, partial(_rename, name=name))

    def _reap_abandoned(self, instances: dict[str, BrevInstance]) -> None:
        """Delete managed instances no active session owns, including ones left after a restart."""
        owned = self._store.owned_instance_names()
        for name in instances:
            if not is_managed_instance_name(name) or name in owned:
                continue
            logger.warning("Deleting abandoned Brev instance %s", name)
            try:
                self._brev.delete(name)
            except BrevError as exc:
                logger.warning("brev delete of abandoned instance %s failed: %s", name, exc)
            except Exception:
                logger.exception("Unexpected error deleting abandoned instance %s", name)
            finally:
                self._beat()

    def _update(self, claimed: ClaimedSession, change: Callable) -> None:
        try:
            with self._store.locked(claimed.id) as record:
                if record is not None:
                    change(record)
        except Exception:
            logger.exception("Failed to update session %s", claimed.id)


def _rename(record, *, name: str) -> None:
    if record.status not in TERMINAL_STATUSES:
        record.instance_name = name


def _log_extra(session_id, instance_name: str, commit_sha: str) -> dict[str, str]:
    return {"session_id": str(session_id), "instance_name": instance_name, "commit_sha": commit_sha}


def write_heartbeat(path: Path) -> None:
    """Atomically write the current UTC time so the container healthcheck can see the worker."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(utcnow().isoformat())
    os.replace(tmp, path)


def run(worker: Worker, *, interval_s: float, beat: Callable[[], None], stop: threading.Event):
    """Call `tick` until `stop` is set. One failed tick does not stop the loop."""
    while not stop.is_set():
        started = time.monotonic()
        try:
            worker.tick()
        except Exception:
            logger.exception("Worker tick failed")
        beat()
        stop.wait(max(0.0, interval_s - (time.monotonic() - started)))


def main() -> int:
    """Start the worker. Returns 2 when `BREV_API_KEY` is missing."""
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if settings.brev_api_key is None:
        logger.error("BREV_API_KEY is required for the worker")
        return 2

    engine = build_engine(settings)
    try:
        check_database(engine)
        heartbeat_path = Path(settings.worker_heartbeat_path)

        def beat() -> None:
            try:
                write_heartbeat(heartbeat_path)
            except OSError:
                logger.exception("Could not write heartbeat %s", heartbeat_path)

        brev = CliBrevClient(
            api_key=settings.brev_api_key.get_secret_value(),
            cli_path=settings.brev_cli_path,
            timeout_s=settings.brev_command_timeout_s,
        )
        worker = Worker(
            SqlSessionStore(engine),
            brev,
            launchable_id=settings.brev_launchable_id,
            beat=beat,
            timeouts=Timeouts(
                startup=timedelta(seconds=settings.session_startup_timeout_s),
                idle=timedelta(seconds=settings.session_idle_timeout_s),
                max_duration=timedelta(seconds=settings.session_max_duration_s),
            ),
        )
        stop = threading.Event()

        def handle_signal(signum, _frame) -> None:
            logger.info("Received %s; exiting after the current step", signal.Signals(signum).name)
            stop.set()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        logger.info(
            "Worker started: launchable %s, poll every %ss",
            settings.brev_launchable_id,
            settings.worker_poll_interval_s,
        )
        run(worker, interval_s=settings.worker_poll_interval_s, beat=beat, stop=stop)
        logger.info("Worker stopped")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
