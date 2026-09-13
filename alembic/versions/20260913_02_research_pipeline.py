"""Add durable V1.3 research inbox and evidence persistence.

Revision ID: 20260913_02
Revises: 20260913_01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_02"
down_revision: Union[str, Sequence[str], None] = "20260913_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_inbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=False),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("pipeline_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("materials", sa.JSON(), nullable=False),
        sa.Column("python_metrics", sa.JSON(), nullable=False),
        sa.Column("quality_snapshot", sa.JSON(), nullable=False),
        sa.Column("research_packet", sa.JSON(), nullable=False),
        sa.Column("structured_packet", sa.JSON(), nullable=False),
        sa.Column("decision_plan", sa.JSON(), nullable=False),
        sa.Column("candidate_order_ids", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "idempotency_key",
            name="uq_research_inbox_account_idempotency",
        ),
    )
    op.create_index("ix_research_inbox_account_id", "research_inbox", ["account_id"])
    op.create_index("ix_research_inbox_fund_id", "research_inbox", ["fund_id"])
    op.create_index(
        "ix_research_inbox_status_created",
        "research_inbox",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_research_inbox_fund_status",
        "research_inbox",
        ["fund_id", "status"],
    )

    op.create_table(
        "research_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("research_item_id", sa.String(length=36), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=128), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("source_name", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_counter_evidence", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.ForeignKeyConstraint(["research_item_id"], ["research_inbox.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "research_item_id",
            "evidence_id",
            name="uq_research_evidence_item_evidence",
        ),
    )
    op.create_index(
        "ix_research_evidence_research_item_id",
        "research_evidence",
        ["research_item_id"],
    )
    op.create_index("ix_research_evidence_fund_id", "research_evidence", ["fund_id"])
    op.create_index(
        "ix_research_evidence_fund_observed",
        "research_evidence",
        ["fund_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_evidence_fund_observed", table_name="research_evidence")
    op.drop_index("ix_research_evidence_fund_id", table_name="research_evidence")
    op.drop_index("ix_research_evidence_research_item_id", table_name="research_evidence")
    op.drop_table("research_evidence")

    op.drop_index("ix_research_inbox_fund_status", table_name="research_inbox")
    op.drop_index("ix_research_inbox_status_created", table_name="research_inbox")
    op.drop_index("ix_research_inbox_fund_id", table_name="research_inbox")
    op.drop_index("ix_research_inbox_account_id", table_name="research_inbox")
    op.drop_table("research_inbox")
