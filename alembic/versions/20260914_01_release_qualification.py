"""Add durable V1.3 release qualification journal.

Revision ID: 20260914_01
Revises: 20260913_07
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260914_01"
down_revision: Union[str, Sequence[str], None] = "20260913_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "release_qualification_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_version", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("suite_version", sa.String(length=40), nullable=False),
        sa.Column("observed_start_date", sa.Date(), nullable=True),
        sa.Column("observed_end_date", sa.Date(), nullable=True),
        sa.Column("calendar_days", sa.Integer(), nullable=False),
        sa.Column("min_business_days", sa.Integer(), nullable=False),
        sa.Column("enabled_account_count", sa.Integer(), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_release_qualification_mode_created",
        "release_qualification_run",
        ["mode", "created_at"],
    )
    op.create_index(
        "ix_release_qualification_status_created",
        "release_qualification_run",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_release_qualification_status_created",
        table_name="release_qualification_run",
    )
    op.drop_index(
        "ix_release_qualification_mode_created",
        table_name="release_qualification_run",
    )
    op.drop_table("release_qualification_run")
