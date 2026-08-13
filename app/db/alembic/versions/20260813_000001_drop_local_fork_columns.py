"""Drop columns created by the production fork but absent from official ORM.

The 2455 production fork added six columns via local-only migrations:
- accounts.stream_limit_override / response_create_limit_override
  (account concurrency overrides)
- api_keys.account_assignment_generation / account_assignment_changed_at
  (api-key assignment generation)
- proxy_pools.overflow_threshold / routing_strategy
  (proxy pool overflow policy)

The official ORM does not model these; its startup schema-drift check refuses
to boot while they exist. These features are being retired with the fork, so
drop the columns outright. Values are lost by design.

Revision ID: 20260813_000001_drop_local_fork_columns
Revises: 20260813_000000_merge_local_proxy_pool_lineage
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260813_000001_drop_local_fork_columns"
down_revision = "20260813_000000_merge_local_proxy_pool_lineage"
branch_labels = None
depends_on = None

_ORIGINAL_COLUMNS = {
    "accounts": [
        sa.Column("stream_limit_override", sa.Integer(), nullable=True),
        sa.Column("response_create_limit_override", sa.Integer(), nullable=True),
    ],
    "api_keys": [
        sa.Column(
            "account_assignment_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("account_assignment_changed_at", sa.DateTime(), nullable=True),
    ],
    "proxy_pools": [
        sa.Column(
            "routing_strategy",
            sa.String(),
            nullable=False,
            server_default=sa.text("'failover'"),
        ),
        sa.Column("overflow_threshold", sa.Integer(), nullable=True),
    ],
}


def upgrade() -> None:
    for table, columns in _ORIGINAL_COLUMNS.items():
        with op.batch_alter_table(table) as batch_op:
            for column in columns:
                batch_op.drop_column(column.name)


def downgrade() -> None:
    for table, columns in _ORIGINAL_COLUMNS.items():
        with op.batch_alter_table(table) as batch_op:
            for column in columns:
                batch_op.add_column(column)
