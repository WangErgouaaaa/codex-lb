"""add latest_checkpoint_response_id pointer to http bridge sessions

Revision ID: 20260813_000003_add_http_bridge_checkpoint_pointer
Revises: 20260813_000002_add_http_bridge_checkpoints
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260813_000003_add_http_bridge_checkpoint_pointer"
down_revision = "20260813_000002_add_http_bridge_checkpoints"
branch_labels = None
depends_on = None

_TABLE = "http_bridge_sessions"
_COLUMN = "latest_checkpoint_response_id"


def _has_table(connection: Connection) -> bool:
    return sa.inspect(connection).has_table(_TABLE)


def _has_column(connection: Connection) -> bool:
    return any(item["name"] == _COLUMN for item in sa.inspect(connection).get_columns(_TABLE))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind) or _has_column(bind):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind) and _has_column(bind):
        op.drop_column(_TABLE, _COLUMN)
