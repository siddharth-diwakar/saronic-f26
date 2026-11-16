"""Register each session's simulator bridge connection on sim_sessions.

The worker keeps an SSH tunnel to the instance's control bridge and records where the API
reaches it, which simulator process answered (runtime ID), and whether it is ready. Each
session gets a bridge token; existing rows are given one so a session that is running when
this deploys can still be connected.

Revision ID: 185799806ea6
Revises: a4d8c1e06b33
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "185799806ea6"
down_revision: str | Sequence[str] | None = "a4d8c1e06b33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sim_sessions", sa.Column("bridge_token", sa.Text(), nullable=True))
    # gen_random_uuid() draws from PostgreSQL's strong random source; two give 244 random bits.
    op.execute(
        "UPDATE sim_sessions SET bridge_token = "
        "replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', '')"
    )
    op.alter_column("sim_sessions", "bridge_token", nullable=False)
    op.add_column("sim_sessions", sa.Column("bridge_host", sa.Text(), nullable=True))
    op.add_column("sim_sessions", sa.Column("bridge_port", sa.Integer(), nullable=True))
    op.add_column("sim_sessions", sa.Column("bridge_runtime_id", sa.Text(), nullable=True))
    op.add_column(
        "sim_sessions",
        sa.Column("bridge_ready", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("sim_sessions", sa.Column("bridge_error", sa.Text(), nullable=True))
    op.add_column(
        "sim_sessions",
        sa.Column("bridge_connected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sim_sessions",
        sa.Column("bridge_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "bridge_checked_at",
        "bridge_connected_at",
        "bridge_error",
        "bridge_ready",
        "bridge_runtime_id",
        "bridge_port",
        "bridge_host",
        "bridge_token",
    ):
        op.drop_column("sim_sessions", column)
