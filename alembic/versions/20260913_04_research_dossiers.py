"""Add multi-source research dossier assembly.

Revision ID: 20260913_04
Revises: 20260913_03
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_04"
down_revision: Union[str, Sequence[str], None] = "20260913_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_dossier",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("research_item_id", sa.String(length=36), nullable=False),
        sa.Column("selected_document_ids", sa.JSON(), nullable=False),
        sa.Column("suppressed_document_ids", sa.JSON(), nullable=False),
        sa.Column("raw_research_item_ids", sa.JSON(), nullable=False),
        sa.Column("source_names", sa.JSON(), nullable=False),
        sa.Column("material_count", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("total_chars", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.ForeignKeyConstraint(["research_item_id"], ["research_inbox.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "fund_id",
            "business_date",
            name="uq_research_dossier_account_fund_day",
        ),
    )
    op.create_index("ix_research_dossier_account_id", "research_dossier", ["account_id"])
    op.create_index("ix_research_dossier_fund_id", "research_dossier", ["fund_id"])
    op.create_index("ix_research_dossier_research_item_id", "research_dossier", ["research_item_id"])
    op.create_index(
        "ix_research_dossier_status_date",
        "research_dossier",
        ["status", "business_date"],
    )

    with op.batch_alter_table("research_collected_document") as batch_op:
        batch_op.add_column(sa.Column("dossier_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_research_collected_document_dossier_id",
            "research_dossier",
            ["dossier_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_research_collected_document_dossier_id",
            ["dossier_id"],
        )
        batch_op.create_index(
            "ix_research_collected_dossier_status",
            ["dossier_id", "status"],
        )


def downgrade() -> None:
    with op.batch_alter_table("research_collected_document") as batch_op:
        batch_op.drop_index("ix_research_collected_dossier_status")
        batch_op.drop_index("ix_research_collected_document_dossier_id")
        batch_op.drop_constraint(
            "fk_research_collected_document_dossier_id",
            type_="foreignkey",
        )
        batch_op.drop_column("dossier_id")

    op.drop_index("ix_research_dossier_status_date", table_name="research_dossier")
    op.drop_index("ix_research_dossier_research_item_id", table_name="research_dossier")
    op.drop_index("ix_research_dossier_fund_id", table_name="research_dossier")
    op.drop_index("ix_research_dossier_account_id", table_name="research_dossier")
    op.drop_table("research_dossier")
