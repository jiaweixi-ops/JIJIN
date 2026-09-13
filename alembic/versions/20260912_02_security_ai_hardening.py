"""Add credential lifecycle fields and AI usage ledger.

Revision ID: 20260912_02
Revises: 20260912_01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260912_02"
down_revision: Union[str, Sequence[str], None] = "20260912_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("api_credential") as batch:
        batch.add_column(sa.Column("hash_version", sa.String(length=32), nullable=False, server_default="sha256"))
        batch.add_column(sa.Column("token_prefix", sa.String(length=16), nullable=False, server_default=""))
        batch.add_column(sa.Column("created_by", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("rotated_from_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_api_credential_rotated_from",
            "api_credential",
            ["rotated_from_id"],
            ["id"],
        )
        batch.create_index("ix_api_credential_rotated_from_id", ["rotated_from_id"])
        batch.create_index("ix_api_credential_expires_at", ["expires_at"])
        batch.create_index("ix_api_credential_revoked_at", ["revoked_at"])

    op.create_table(
        "ai_usage_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("quota_subject", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("role_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("input_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("cost_currency", sa.String(length=8), nullable=False, server_default="CNY"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("denied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_ledger_quota_subject", "ai_usage_ledger", ["quota_subject"])
    op.create_index("ix_ai_usage_ledger_request_id", "ai_usage_ledger", ["request_id"])
    op.create_index("ix_ai_usage_ledger_created_at", "ai_usage_ledger", ["created_at"])
    op.create_index(
        "ix_ai_usage_subject_created",
        "ai_usage_ledger",
        ["quota_subject", "created_at"],
    )
    op.create_index(
        "ix_ai_usage_provider_created",
        "ai_usage_ledger",
        ["provider", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_provider_created", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_subject_created", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_ledger_created_at", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_ledger_request_id", table_name="ai_usage_ledger")
    op.drop_index("ix_ai_usage_ledger_quota_subject", table_name="ai_usage_ledger")
    op.drop_table("ai_usage_ledger")

    with op.batch_alter_table("api_credential") as batch:
        batch.drop_index("ix_api_credential_revoked_at")
        batch.drop_index("ix_api_credential_expires_at")
        batch.drop_index("ix_api_credential_rotated_from_id")
        batch.drop_constraint("fk_api_credential_rotated_from", type_="foreignkey")
        batch.drop_column("revoked_at")
        batch.drop_column("expires_at")
        batch.drop_column("rotated_from_id")
        batch.drop_column("created_by")
        batch.drop_column("token_prefix")
        batch.drop_column("hash_version")
