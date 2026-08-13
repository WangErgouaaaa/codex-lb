"""add canonical account-neutral continuation checkpoints

Revision ID: 20260813_000002_add_http_bridge_checkpoints
Revises: 20260813_000001_drop_local_fork_columns
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260813_000002_add_http_bridge_checkpoints"
down_revision = "20260813_000001_drop_local_fork_columns"
branch_labels = None
depends_on = None

_TABLE = "http_bridge_checkpoints"


def _has_table(connection: Connection) -> bool:
    return sa.inspect(connection).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("response_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("api_key_scope", sa.String(255), nullable=False),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("input_item_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["http_bridge_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("response_id"),
    )
    op.create_index("idx_http_bridge_checkpoints_session", _TABLE, ["session_id", "created_at"])
    op.create_index("idx_http_bridge_checkpoints_expires", _TABLE, ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        op.drop_table(_TABLE)
