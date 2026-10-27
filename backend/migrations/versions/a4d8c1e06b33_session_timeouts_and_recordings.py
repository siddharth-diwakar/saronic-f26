"""Record activity and that a recording was saved before delete.

Revision ID: a4d8c1e06b33
Revises: e9155e165f02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d8c1e06b33"
down_revision: str | Sequence[str] | None = "e9155e165f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES = "'bootstrap', 'docker_build', 'isaac_launch', 'failure_tail', 'recording'"
_PREVIOUS_SOURCES = "'bootstrap', 'docker_build', 'isaac_launch', 'failure_tail'"


def upgrade() -> None:
    op.add_column(
        "sim_sessions",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sim_sessions",
        sa.Column("outputs_saved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_session_log_excerpts_source", "session_log_excerpts", type_="check")
    op.create_check_constraint(
        "ck_session_log_excerpts_source",
        "session_log_excerpts",
        f"source IN ({_SOURCES})",
    )


def downgrade() -> None:
    op.execute("DELETE FROM session_log_excerpts WHERE source = 'recording'")
    op.drop_constraint("ck_session_log_excerpts_source", "session_log_excerpts", type_="check")
    op.create_check_constraint(
        "ck_session_log_excerpts_source",
        "session_log_excerpts",
        f"source IN ({_PREVIOUS_SOURCES})",
    )
    op.drop_column("sim_sessions", "outputs_saved_at")
    op.drop_column("sim_sessions", "last_activity_at")
