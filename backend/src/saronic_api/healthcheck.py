"""Container probe without adding an HTTP health route."""

import sys
from urllib.request import urlopen

from sqlalchemy.exc import SQLAlchemyError

from saronic_api.config import Settings
from saronic_api.database import build_engine, check_database


def main() -> int:
    engine = None
    try:
        with urlopen("http://127.0.0.1:8000/openapi.json", timeout=2) as response:
            if response.status != 200:
                return 1
        engine = build_engine(Settings())
        check_database(engine)
        return 0
    except (OSError, ValueError, SQLAlchemyError):
        # Avoid leaking database connection details into Docker health logs.
        print("API or database health check failed", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
