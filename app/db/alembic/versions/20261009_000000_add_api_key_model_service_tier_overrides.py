"""add api key model service tier overrides

Revision ID: 20261009_000000_add_api_key_model_service_tier_overrides
Revises: 20261008_000000_add_model_plan_preferences
Create Date: 2026-10-09

Adds the nullable ``model_service_tier_overrides_json`` column to
``api_keys`` storing ``{"<model-slug>": "auto"|"default"|"priority"|"flex"}``.
When a request's effective model matches an entry, that tier replaces the
key-wide ``enforced_service_tier``; NULL/empty means no per-model overrides.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "20261009_000000_add_api_key_model_service_tier_overrides"
down_revision = "20261008_000000_add_model_plan_preferences"
branch_labels = None
depends_on = None


def _columns(connection: Connection, table_name: str) -> set[str]:
    inspector = sa.inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name) if column.get("name") is not None}


def upgrade() -> None:
    bind = op.get_bind()
    api_key_columns = _columns(bind, "api_keys")
    if not api_key_columns:
        return
    if "model_service_tier_overrides_json" in api_key_columns:
        return

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(
            sa.Column(
                "model_service_tier_overrides_json",
                sa.Text(),
                server_default=None,
                nullable=True,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    api_key_columns = _columns(bind, "api_keys")
    if not api_key_columns:
        return
    if "model_service_tier_overrides_json" not in api_key_columns:
        return

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_column("model_service_tier_overrides_json")
