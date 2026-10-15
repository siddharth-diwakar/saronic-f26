"""Shared SQLAlchemy engine, session dependency, and model metadata."""

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session

from saronic_api.config import Settings


class Base(DeclarativeBase):
    pass


def database_url(settings: Settings) -> URL:
    # Coolify supplies postgresql://; select psycopg 3 explicitly.
    return make_url(settings.database_url.get_secret_value()).set(drivername="postgresql+psycopg")


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        database_url(settings),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )


def check_database(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def get_db(request: Request) -> Generator[Session, None, None]:
    # Future synchronous route handlers can use Depends(get_db).
    # Callers own transaction commits; closing rolls back uncommitted work.
    with Session(request.app.state.engine) as session:
        yield session
