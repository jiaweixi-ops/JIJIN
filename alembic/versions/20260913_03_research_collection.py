"""Add external research collection sources, runs and normalized documents.

Revision ID: 20260913_03
Revises: 20260913_02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_03"
down_revision: Union[str, Sequence[str], None] = "20260913_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_collection_source",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("topic_prefix", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("last_modified", sa.String(length=512), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "fund_id",
            "feed_url",
            name="uq_research_collection_source_target_url",
        ),
    )
    op.create_index(
        "ix_research_collection_source_account_id",
        "research_collection_source",
        ["account_id"],
    )
    op.create_index(
        "ix_research_collection_source_fund_id",
        "research_collection_source",
        ["fund_id"],
    )
    op.create_index(
        "ix_research_collection_source_enabled",
        "research_collection_source",
        ["enabled", "updated_at"],
    )

    op.create_table(
        "research_collection_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("ingested_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["research_collection_source.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_research_collection_run_source_id",
        "research_collection_run",
        ["source_id"],
    )
    op.create_index(
        "ix_research_collection_run_source_started",
        "research_collection_run",
        ["source_id", "started_at"],
    )
    op.create_index(
        "ix_research_collection_run_status_started",
        "research_collection_run",
        ["status", "started_at"],
    )

    op.create_table(
        "research_collected_document",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("research_item_id", sa.String(length=36), nullable=True),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["research_item_id"], ["research_inbox.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["research_collection_source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "fingerprint",
            name="uq_research_collected_source_fingerprint",
        ),
    )
    op.create_index(
        "ix_research_collected_document_source_id",
        "research_collected_document",
        ["source_id"],
    )
    op.create_index(
        "ix_research_collected_document_research_item_id",
        "research_collected_document",
        ["research_item_id"],
    )
    op.create_index(
        "ix_research_collected_source_observed",
        "research_collected_document",
        ["source_id", "observed_at"],
    )
    op.create_index(
        "ix_research_collected_status_created",
        "research_collected_document",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_research_collected_status_created", table_name="research_collected_document")
    op.drop_index("ix_research_collected_source_observed", table_name="research_collected_document")
    op.drop_index("ix_research_collected_document_research_item_id", table_name="research_collected_document")
    op.drop_index("ix_research_collected_document_source_id", table_name="research_collected_document")
    op.drop_table("research_collected_document")

    op.drop_index("ix_research_collection_run_status_started", table_name="research_collection_run")
    op.drop_index("ix_research_collection_run_source_started", table_name="research_collection_run")
    op.drop_index("ix_research_collection_run_source_id", table_name="research_collection_run")
    op.drop_table("research_collection_run")

    op.drop_index("ix_research_collection_source_enabled", table_name="research_collection_source")
    op.drop_index("ix_research_collection_source_fund_id", table_name="research_collection_source")
    op.drop_index("ix_research_collection_source_account_id", table_name="research_collection_source")
    op.drop_table("research_collection_source")
