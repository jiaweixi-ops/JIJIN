"""Add durable operational alert lifecycle.

Revision ID: 20260913_05
Revises: 20260913_04
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_05"
down_revision: Union[str, Sequence[str], None] = "20260913_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operational_alert",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=220), nullable=False),
        sa.Column("alert_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=128), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_operational_alert_dedupe_key"),
    )
    op.create_index(
        "ix_operational_alert_alert_type",
        "operational_alert",
        ["alert_type"],
    )
    op.create_index(
        "ix_operational_alert_state_severity",
        "operational_alert",
        ["state", "severity"],
    )
    op.create_index(
        "ix_operational_alert_scope",
        "operational_alert",
        ["scope_type", "scope_id"],
    )
    op.create_index(
        "ix_operational_alert_last_seen",
        "operational_alert",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operational_alert_last_seen", table_name="operational_alert")
    op.drop_index("ix_operational_alert_scope", table_name="operational_alert")
    op.drop_index("ix_operational_alert_state_severity", table_name="operational_alert")
    op.drop_index("ix_operational_alert_alert_type", table_name="operational_alert")
    op.drop_table("operational_alert")
