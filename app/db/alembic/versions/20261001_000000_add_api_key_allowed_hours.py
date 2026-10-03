"""add api key allowed hours window

Revision ID: 20261001_000000_add_api_key_allowed_hours
Revises: 20260921_000000_add_request_log_actual_model
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20261001_000000_add_api_key_allowed_hours"
down_revision = "20260921_000000_add_request_log_actual_model"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "api_keys")
    if not columns:
        return
    with op.batch_alter_table("api_keys") as batch_op:
        if "allowed_hours_start" not in columns:
            batch_op.add_column(sa.Column("allowed_hours_start", sa.String(), nullable=True))
        if "allowed_hours_end" not in columns:
            batch_op.add_column(sa.Column("allowed_hours_end", sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "api_keys")
    if not columns:
        return
    with op.batch_alter_table("api_keys") as batch_op:
        if "allowed_hours_start" in columns:
            batch_op.drop_column("allowed_hours_start")
        if "allowed_hours_end" in columns:
            batch_op.drop_column("allowed_hours_end")
