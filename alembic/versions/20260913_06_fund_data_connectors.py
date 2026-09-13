"""Add registered fund-data connectors and sync audit persistence.

Revision ID: 20260913_06
Revises: 20260913_05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260913_06"
down_revision: Union[str, Sequence[str], None] = "20260913_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fund_data_connector",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("auth_header_name", sa.String(length=100), nullable=True),
        sa.Column("auth_env_key", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("etag", sa.String(length=500), nullable=True),
        sa.Column("last_modified", sa.String(length=500), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_name"], ["data_source.name"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_fund_data_connector_name"),
    )
    op.create_index("ix_fund_data_connector_source_name", "fund_data_connector", ["source_name"])
    op.create_index("ix_fund_data_connector_enabled", "fund_data_connector", ["enabled"])

    op.create_table(
        "fund_data_sync_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("ingested_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connector_id"], ["fund_data_connector.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_data_sync_run_connector_id", "fund_data_sync_run", ["connector_id"])
    op.create_index(
        "ix_fund_data_sync_run_connector_started",
        "fund_data_sync_run",
        ["connector_id", "started_at"],
    )

    op.create_table(
        "fund_data_observation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("fund_id", sa.String(length=36), nullable=True),
        sa.Column("fund_code", sa.String(length=20), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connector_id"], ["fund_data_connector.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["fund_data_sync_run.id"]),
        sa.ForeignKeyConstraint(["fund_id"], ["fund.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connector_id",
            "payload_hash",
            name="uq_fund_data_observation_payload",
        ),
    )
    op.create_index("ix_fund_data_observation_connector_id", "fund_data_observation", ["connector_id"])
    op.create_index("ix_fund_data_observation_run_id", "fund_data_observation", ["run_id"])
    op.create_index("ix_fund_data_observation_source_name", "fund_data_observation", ["source_name"])
    op.create_index("ix_fund_data_observation_fund_id", "fund_data_observation", ["fund_id"])
    op.create_index("ix_fund_data_observation_fund_code", "fund_data_observation", ["fund_code"])
    op.create_index(
        "ix_fund_data_observation_fund_time",
        "fund_data_observation",
        ["fund_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_fund_data_observation_fund_time", table_name="fund_data_observation")
    op.drop_index("ix_fund_data_observation_fund_code", table_name="fund_data_observation")
    op.drop_index("ix_fund_data_observation_fund_id", table_name="fund_data_observation")
    op.drop_index("ix_fund_data_observation_source_name", table_name="fund_data_observation")
    op.drop_index("ix_fund_data_observation_run_id", table_name="fund_data_observation")
    op.drop_index("ix_fund_data_observation_connector_id", table_name="fund_data_observation")
    op.drop_table("fund_data_observation")
    op.drop_index("ix_fund_data_sync_run_connector_started", table_name="fund_data_sync_run")
    op.drop_index("ix_fund_data_sync_run_connector_id", table_name="fund_data_sync_run")
    op.drop_table("fund_data_sync_run")
    op.drop_index("ix_fund_data_connector_enabled", table_name="fund_data_connector")
    op.drop_index("ix_fund_data_connector_source_name", table_name="fund_data_connector")
    op.drop_table("fund_data_connector")
