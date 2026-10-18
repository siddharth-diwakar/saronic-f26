"""Container probe without adding an HTTP health route.

PROCESS_ROLE=api (default) probes the local OpenAPI response; PROCESS_ROLE=worker checks the
worker heartbeat file instead, since the worker has no HTTP server. Both also query the DB.
"""

import sys
import time
from pathlib import Path
from urllib.request import urlopen

from sqlalchemy.exc import SQLAlchemyError

from saronic_api.config import Settings
from saronic_api.database import build_engine, check_database


def main() -> int:
    engine = None
    try:
        settings = Settings()
        if settings.process_role == "worker":
            if not heartbeat_is_fresh(settings):
                print("Worker heartbeat is missing or stale", file=sys.stderr)
                return 1
        else:
            with urlopen("http://127.0.0.1:8000/openapi.json", timeout=2) as response:
                if response.status != 200:
                    return 1
        engine = build_engine(settings)
        check_database(engine)
        return 0
    except (OSError, ValueError, SQLAlchemyError):
        # Avoid leaking database connection details into Docker health logs.
        print("Health check failed (API/worker or database)", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def heartbeat_max_age_s(settings: Settings) -> float:
    # Three poll intervals, plus one Brev CLI timeout: the worker beats after every Brev
    # call, but a single slow call (e.g. create) can take up to the timeout. A bare 3x
    # interval (30 s) would flag a healthy worker during a slow create.
    return 3 * settings.worker_poll_interval_s + settings.brev_command_timeout_s


def heartbeat_is_fresh(settings: Settings) -> bool:
    path = Path(settings.worker_heartbeat_path)
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age <= heartbeat_max_age_s(settings)


if __name__ == "__main__":
    sys.exit(main())
