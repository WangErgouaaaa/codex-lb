"""add dashboard plan concurrency caps

Revision ID: 20261002_000000_add_dashboard_plan_concurrency_caps
Revises: 20261001_020000_add_credit_attribution_tables
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20261002_000000_add_dashboard_plan_concurrency_caps"
down_revision = "20261001_020000_add_credit_attribution_tables"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    dashboard_columns = _columns(bind, "dashboard_settings")
    if not dashboard_columns:
        return
    if "proxy_account_plan_concurrency_caps_json" in dashboard_columns:
        return

    with op.batch_alter_table("dashboard_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "proxy_account_plan_concurrency_caps_json",
                sa.Text(),
                server_default="{}",
                nullable=False,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    dashboard_columns = _columns(bind, "dashboard_settings")
    if not dashboard_columns:
        return
    if "proxy_account_plan_concurrency_caps_json" not in dashboard_columns:
        return

    with op.batch_alter_table("dashboard_settings") as batch_op:
        batch_op.drop_column("proxy_account_plan_concurrency_caps_json")
