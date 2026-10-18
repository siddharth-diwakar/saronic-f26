"""PostgreSQL fixtures. Tests using `db_engine` are skipped unless TEST_DATABASE_URL is set.

Point TEST_DATABASE_URL at a throwaway database: it is migrated to head and sim_sessions is
truncated before each test. SQLite can't stand in (SKIP LOCKED, FOR UPDATE, advisory locks).
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def migrated_engine():
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to a throwaway PostgreSQL database")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL  # migrations/env.py reads Settings()
    try:
        command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous
    engine = create_engine(make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def db_engine(migrated_engine):
    with migrated_engine.begin() as connection:
        connection.execute(text("TRUNCATE sim_sessions"))
    return migrated_engine
