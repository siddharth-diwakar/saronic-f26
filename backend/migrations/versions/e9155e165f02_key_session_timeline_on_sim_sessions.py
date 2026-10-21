"""Key the session timeline on sim_sessions and drop the separate sessions table.

sim_sessions (the worker's table) becomes the only session table. session_events,
session_log_excerpts and session_alerts now reference it, and lose `attempt`: a retry is
a new session because Brev instance names are derived from the session ID.

Rows in the three timeline tables belonged to `sessions` rows, which cannot be mapped to
sim_sessions (no instance or resolved commit), so they are deleted. Nothing provisioned
those sessions. sim_sessions gains the latest timeline `step` and `failed_step`.

Revision ID: e9155e165f02
Revises: 0c826a240e31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9155e165f02"
down_revision: str | Sequence[str] | None = "0c826a240e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEPS = (
    "'requested', 'provisioning', 'instance_up', 'container_built', 'scene_loaded', "
    "'physics_running', 'cameras_ready', 'ready', 'failed', 'teardown_requested', 'stopped'"
)
_FORWARD = (
    "'requested', 'provisioning', 'instance_up', 'container_built', 'scene_loaded', "
    "'physics_running', 'cameras_ready', 'ready'"
)
# (table, old unique constraint, new unique constraint, unique columns without attempt)
_TIMELINE_TABLES = (
    (
        "session_events",
        "uq_session_events_session_attempt_step",
        "uq_session_events_session_step",
        ["session_id", "step"],
    ),
    (
        "session_log_excerpts",
        "uq_session_log_excerpts_session_attempt_source",
        "uq_session_log_excerpts_session_source",
        ["session_id", "source"],
    ),
    (
        "session_alerts",
        "uq_session_alerts_session_attempt_kind_step",
        "uq_session_alerts_session_kind_step",
        ["session_id", "kind", "step"],
    ),
)


def upgrade() -> None:
    for table, old_unique, new_unique, columns in _TIMELINE_TABLES:
        op.execute(f"DELETE FROM {table}")
        op.drop_constraint(f"{table}_session_id_fkey", table, type_="foreignkey")
        op.drop_constraint(old_unique, table, type_="unique")
        # Also drops the ck_<table>_attempt check.
        op.drop_column(table, "attempt")
        op.create_unique_constraint(new_unique, table, columns)
        op.create_foreign_key(
            f"{table}_session_id_fkey",
            table,
            "sim_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_index("ix_sessions_status_updated_at", table_name="sessions")
    op.drop_table("sessions")

    op.add_column("sim_sessions", sa.Column("step", sa.String(length=32), nullable=True))
    op.add_column("sim_sessions", sa.Column("failed_step", sa.String(length=32), nullable=True))
    op.execute(
        """
        UPDATE sim_sessions SET step = CASE status
            WHEN 'requested' THEN 'requested'
            WHEN 'provisioning' THEN 'provisioning'
            WHEN 'running' THEN 'instance_up'
            WHEN 'stopping' THEN 'teardown_requested'
            ELSE 'stopped'
        END
        """
    )
    op.alter_column("sim_sessions", "step", nullable=False)
    op.create_check_constraint("ck_sim_sessions_step", "sim_sessions", f"step IN ({_STEPS})")
    op.create_check_constraint(
        "ck_sim_sessions_failed_step",
        "sim_sessions",
        f"failed_step IS NULL OR failed_step IN ({_FORWARD})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_sim_sessions_failed_step", "sim_sessions", type_="check")
    op.drop_constraint("ck_sim_sessions_step", "sim_sessions", type_="check")
    op.drop_column("sim_sessions", "failed_step")
    op.drop_column("sim_sessions", "step")

    # As created by b7e1c0a84f22.
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("brev_instance_id", sa.String(length=128), nullable=True),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("failed_step", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({_STEPS})", name="ck_sessions_status"),
        sa.CheckConstraint(
            f"failed_step IS NULL OR failed_step IN ({_FORWARD})",
            name="ck_sessions_failed_step",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_sessions_attempt"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_status_updated_at", "sessions", ["status", "updated_at"])

    for table, old_unique, new_unique, columns in _TIMELINE_TABLES:
        op.execute(f"DELETE FROM {table}")
        op.drop_constraint(f"{table}_session_id_fkey", table, type_="foreignkey")
        op.drop_constraint(new_unique, table, type_="unique")
        op.add_column(table, sa.Column("attempt", sa.Integer(), nullable=False))
        op.create_check_constraint(f"ck_{table}_attempt", table, "attempt >= 1")
        op.create_unique_constraint(old_unique, table, [columns[0], "attempt", *columns[1:]])
        op.create_foreign_key(
            f"{table}_session_id_fkey",
            table,
            "sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
