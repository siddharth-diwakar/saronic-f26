"""Read runtime DB configuration; never store credentials in alembic.ini."""

from alembic import context

import saronic_api.models
import saronic_api.sessions.models  # noqa: F401
from saronic_api.config import Settings
from saronic_api.database import Base, build_engine, database_url

# Import model modules above so autogenerate sees their metadata.
target_metadata = Base.metadata


if context.is_offline_mode():
    context.configure(
        url=database_url(Settings()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = build_engine(Settings())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
