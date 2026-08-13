"""Merge local proxy-pool lineage into official main lineage.

Production 2455 ran on a fork whose head was
20260803_000000_add_proxy_pool_overflow_policy (and the four local-only
migrations leading to it). The official main chain advanced independently to
20260812_000000_merge_recovery_dispatch_and_hourly_cancelled_heads. Both
lineages already apply to the production database, so this merge is a no-op
that makes the combined history linear for future upgrades.

Revision ID: 20260813_000000_merge_local_proxy_pool_lineage
Revises: 20260803_000000_add_proxy_pool_overflow_policy,
         20260812_000000_merge_recovery_dispatch_and_hourly_cancelled_heads
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "20260813_000000_merge_local_proxy_pool_lineage"
down_revision = (
    "20260803_000000_add_proxy_pool_overflow_policy",
    "20260806_000000_add_anonymous_telemetry",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
