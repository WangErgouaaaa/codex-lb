"""Add usage-window snapshot and credit attribution tables.

Snapshots persist the per-account rate-limit window state observed on every
proxied response; the attribution pass differences consecutive snapshots to
estimate real subscription credit consumption per request.

Table creation is guarded with inspector checks because fixture paths may
have already created the tables via ``create_all`` before migrations run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20261001_020000_add_credit_attribution_tables"
down_revision = "20261001_010000_request_logs_missing_cost_index"
branch_labels = None
depends_on = None


def _table_exists(connection: Connection, table_name: str) -> bool:
    inspector = sa.inspect(connection)
    return inspector.has_table(table_name)


def _index_exists(connection: Connection, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return False
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _create_snapshot_table(connection: Connection) -> None:
    if _table_exists(connection, "usage_window_snapshots"):
        return
    op.create_table(
        "usage_window_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("chatgpt_account_id", sa.String(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("request_log_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default=sa.text("'response'")),
        sa.Column("primary_used_percent", sa.Float(), nullable=True),
        sa.Column("primary_window_minutes", sa.Integer(), nullable=True),
        sa.Column("primary_reset_at", sa.DateTime(), nullable=True),
        sa.Column("secondary_used_percent", sa.Float(), nullable=True),
        sa.Column("secondary_window_minutes", sa.Integer(), nullable=True),
        sa.Column("secondary_reset_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_usage_snapshots_account_captured",
        "usage_window_snapshots",
        ["account_id", "captured_at", "id"],
    )


def _create_attribution_table(connection: Connection) -> None:
    if _table_exists(connection, "request_credit_attributions"):
        return
    op.create_table(
        "request_credit_attributions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "request_log_id",
            sa.Integer(),
            sa.ForeignKey("request_logs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("window", sa.String(length=16), nullable=False),
        sa.Column("credits", sa.Float(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("attributed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_log_id", "window", name="uq_credit_attribution_request_window"),
    )
    op.create_index(
        "ix_credit_attributions_account_at",
        "request_credit_attributions",
        ["account_id", "attributed_at"],
    )


def upgrade() -> None:
    connection = op.get_bind()
    _create_snapshot_table(connection)
    _create_attribution_table(connection)
    if not _index_exists(connection, "usage_window_snapshots", "ix_usage_snapshots_account_captured"):
        op.create_index(
            "ix_usage_snapshots_account_captured",
            "usage_window_snapshots",
            ["account_id", "captured_at", "id"],
        )
    if not _index_exists(connection, "request_credit_attributions", "ix_credit_attributions_account_at"):
        op.create_index(
            "ix_credit_attributions_account_at",
            "request_credit_attributions",
            ["account_id", "attributed_at"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    if _table_exists(connection, "request_credit_attributions"):
        op.drop_index("ix_credit_attributions_account_at", table_name="request_credit_attributions")
        op.drop_table("request_credit_attributions")
    if _table_exists(connection, "usage_window_snapshots"):
        op.drop_index("ix_usage_snapshots_account_captured", table_name="usage_window_snapshots")
        op.drop_table("usage_window_snapshots")
