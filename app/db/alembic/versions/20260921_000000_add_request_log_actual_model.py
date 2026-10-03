"""add actual model to request_logs

Revision ID: 20260921_000000_add_request_log_actual_model
Revises: 20260813_000003_add_http_bridge_checkpoint_pointer
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

# revision identifiers, used by Alembic.
revision = "20260921_000000_add_request_log_actual_model"
down_revision = "20260813_000003_add_http_bridge_checkpoint_pointer"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "request_logs")
    if not columns:
        return

    with op.batch_alter_table("request_logs") as batch_op:
        if "actual_model" not in columns:
            batch_op.add_column(sa.Column("actual_model", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "request_logs")
    if not columns:
        return

    with op.batch_alter_table("request_logs") as batch_op:
        if "actual_model" in columns:
            batch_op.drop_column("actual_model")
