"""Session timeline tables.

Revision ID: b7e1c0a84f22
Revises:
Create Date: 2026-10-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e1c0a84f22"
down_revision: str | Sequence[str] | None = None
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
_SOURCES = "'bootstrap', 'docker_build', 'isaac_launch', 'failure_tail'"
_KINDS = "'failed', 'stuck'"


def upgrade() -> None:
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
    op.create_table(
        "session_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("brev_instance_id", sa.String(length=128), nullable=True),
        sa.Column("git_commit", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("failed_step", sa.String(length=32), nullable=True),
        sa.CheckConstraint(f"step IN ({_STEPS})", name="ck_session_events_step"),
        sa.CheckConstraint(
            f"failed_step IS NULL OR failed_step IN ({_FORWARD})",
            name="ck_session_events_failed_step",
        ),
        sa.CheckConstraint("attempt >= 1", name="ck_session_events_attempt"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "attempt", "step", name="uq_session_events_session_attempt_step"
        ),
    )
    op.create_index(
        "ix_session_events_session_occurred", "session_events", ["session_id", "occurred_at"]
    )
    op.create_table(
        "session_log_excerpts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"source IN ({_SOURCES})", name="ck_session_log_excerpts_source"),
        sa.CheckConstraint("attempt >= 1", name="ck_session_log_excerpts_attempt"),
        sa.CheckConstraint("line_count >= 0", name="ck_session_log_excerpts_line_count"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "attempt",
            "source",
            name="uq_session_log_excerpts_session_attempt_source",
        ),
    )
    op.create_table(
        "session_alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_session_alerts_kind"),
        sa.CheckConstraint(f"step IN ({_STEPS})", name="ck_session_alerts_step"),
        sa.CheckConstraint("attempt >= 1", name="ck_session_alerts_attempt"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "attempt",
            "kind",
            "step",
            name="uq_session_alerts_session_attempt_kind_step",
        ),
    )


def downgrade() -> None:
    op.drop_table("session_alerts")
    op.drop_table("session_log_excerpts")
    op.drop_index("ix_session_events_session_occurred", table_name="session_events")
    op.drop_table("session_events")
    op.drop_index("ix_sessions_status_updated_at", table_name="sessions")
    op.drop_table("sessions")
