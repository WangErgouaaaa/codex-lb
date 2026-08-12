"""add proxy pool overflow policy

Revision ID: 20260803_000000_add_proxy_pool_overflow_policy
Revises: 20260727_000000_add_api_key_assignment_generation
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20260803_000000_add_proxy_pool_overflow_policy"
down_revision = "20260727_000000_add_api_key_assignment_generation"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "proxy_pools")
    if not columns:
        return
    with op.batch_alter_table("proxy_pools") as batch_op:
        if "routing_strategy" not in columns:
            batch_op.add_column(
                sa.Column(
                    "routing_strategy",
                    sa.String(),
                    nullable=False,
                    server_default=sa.text("'failover'"),
                )
            )
        if "overflow_threshold" not in columns:
            batch_op.add_column(sa.Column("overflow_threshold", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "proxy_pools")
    if not columns:
        return
    with op.batch_alter_table("proxy_pools") as batch_op:
        if "overflow_threshold" in columns:
            batch_op.drop_column("overflow_threshold")
        if "routing_strategy" in columns:
            batch_op.drop_column("routing_strategy")
