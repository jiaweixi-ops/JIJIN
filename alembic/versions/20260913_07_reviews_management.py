"""Add forward decision reviews, AI contribution scores and management reports.

Revision ID: 20260913_07
Revises: 20260913_06
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_07"
down_revision: Union[str, Sequence[str], None] = "20260913_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "decision_review",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("research_item_id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=True),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(8, 6), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("decision_at", sa.DateTime(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("reference_nav_date", sa.Date(), nullable=True),
        sa.Column("reference_nav", sa.Numeric(18, 8), nullable=True),
        sa.Column("resolved_nav_date", sa.Date(), nullable=True),
        sa.Column("resolved_nav", sa.Numeric(18, 8), nullable=True),
        sa.Column("forward_return", sa.Numeric(18, 10), nullable=True),
        sa.Column("directional_hit", sa.Boolean(), nullable=True),
        sa.Column("calibration_error", sa.Numeric(18, 10), nullable=True),
        sa.Column("execution_outcome", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["research_item_id"], ["research_inbox.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("research_item_id", "horizon_days", name="uq_decision_review_item_horizon"),
    )
    op.create_index("ix_decision_review_research_item_id", "decision_review", ["research_item_id"])
    op.create_index("ix_decision_review_order_id", "decision_review", ["order_id"])
    op.create_index("ix_decision_review_account_id", "decision_review", ["account_id"])
    op.create_index("ix_decision_review_fund_id", "decision_review", ["fund_id"])
    op.create_index("ix_decision_review_decision_id", "decision_review", ["decision_id"])
    op.create_index("ix_decision_review_status_target", "decision_review", ["status", "target_date"])
    op.create_index("ix_decision_review_account_decision", "decision_review", ["account_id", "decision_at"])

    op.create_table(
        "ai_contribution_score",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("role_name", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("domain", sa.String(length=100), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("call_success_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("evidence_traceability_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("directional_hit_rate", sa.Numeric(8, 6), nullable=True),
        sa.Column("confidence_calibration_score", sa.Numeric(8, 6), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("total_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "role_name",
            "period_start",
            "period_end",
            "domain",
            name="uq_ai_contribution_role_period_domain",
        ),
    )
    op.create_index("ix_ai_contribution_period", "ai_contribution_score", ["period_end", "role_name"])

    op.create_table(
        "management_report",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("report_type", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("confirmed_start_assets", sa.Numeric(18, 4), nullable=True),
        sa.Column("confirmed_end_assets", sa.Numeric(18, 4), nullable=True),
        sa.Column("confirmed_period_pnl", sa.Numeric(18, 4), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "report_type",
            "period_start",
            "period_end",
            "revision",
            name="uq_management_report_account_period_revision",
        ),
    )
    op.create_index("ix_management_report_account_id", "management_report", ["account_id"])
    op.create_index("ix_management_report_period", "management_report", ["period_end", "report_type"])


def downgrade() -> None:
    op.drop_index("ix_management_report_period", table_name="management_report")
    op.drop_index("ix_management_report_account_id", table_name="management_report")
    op.drop_table("management_report")
    op.drop_index("ix_ai_contribution_period", table_name="ai_contribution_score")
    op.drop_table("ai_contribution_score")
    op.drop_index("ix_decision_review_account_decision", table_name="decision_review")
    op.drop_index("ix_decision_review_status_target", table_name="decision_review")
    op.drop_index("ix_decision_review_decision_id", table_name="decision_review")
    op.drop_index("ix_decision_review_fund_id", table_name="decision_review")
    op.drop_index("ix_decision_review_account_id", table_name="decision_review")
    op.drop_index("ix_decision_review_order_id", table_name="decision_review")
    op.drop_index("ix_decision_review_research_item_id", table_name="decision_review")
    op.drop_table("decision_review")
