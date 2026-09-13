"""Add durable operational run journal for V1.3 workflows.

Revision ID: 20260913_01
Revises: 20260912_02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_01"
down_revision: Union[str, Sequence[str], None] = "20260912_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operational_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="scheduler"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="RUNNING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scheduled_for", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_name",
            "business_date",
            name="uq_operational_run_job_business_date",
        ),
    )
    op.create_index(
        "ix_operational_run_job_name",
        "operational_run",
        ["job_name"],
    )
    op.create_index(
        "ix_operational_run_business_date",
        "operational_run",
        ["business_date"],
    )
    op.create_index(
        "ix_operational_run_status_date",
        "operational_run",
        ["status", "business_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_operational_run_status_date", table_name="operational_run")
    op.drop_index("ix_operational_run_business_date", table_name="operational_run")
    op.drop_index("ix_operational_run_job_name", table_name="operational_run")
    op.drop_table("operational_run")
