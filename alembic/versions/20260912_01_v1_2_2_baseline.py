"""Adopt the V1.2.1 schema and establish the V1.2.2 Alembic baseline.

This revision is intentionally transitional:
- on a fresh database it creates the frozen V1.2.2 baseline schema;
- on an existing pre-Alembic V1.2.1 database it leaves existing tables intact,
  creates only missing baseline tables, and adds FeishuCallback.status_code if needed.

Future schema changes must be expressed as normal Alembic revisions and must not rely
on application startup create_all().

Revision ID: 20260912_01
Revises: None
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260912_01"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _baseline_metadata() -> sa.MetaData:
    md = sa.MetaData()

    role_enum = sa.Enum("READONLY", "EDITOR", "CONFIRMER", "ADMIN", name="role")
    account_type_enum = sa.Enum("SIMULATION", "MANUAL", name="accounttype")
    cash_status_enum = sa.Enum(
        "AVAILABLE", "FROZEN", "IN_TRANSIT", "SETTLED", name="cashstatus"
    )
    order_side_enum = sa.Enum("BUY", "SELL", "CONVERT", "HOLD", "WAIT", name="orderside")
    order_status_enum = sa.Enum(
        "SUGGESTED",
        "PENDING_RISK",
        "RISK_REJECTED",
        "PENDING_CONFIRM",
        "PENDING_EMERGENCY_CONFIRM",
        "MODIFIED",
        "APPROVED",
        "SUBMITTED",
        "IN_TRANSIT",
        "PARTIALLY_CONFIRMED",
        "CONFIRMED",
        "EXECUTION_FAILED",
        "EXPIRED",
        "CANCELLED",
        "MANUAL_RECONCILED",
        name="orderstatus",
    )
    reconciliation_status_enum = sa.Enum(
        "PENDING", "MATCHED", "DIFF", "BLOCKING", "RESOLVED", name="reconciliationstatus"
    )
    data_quality_enum = sa.Enum("GREEN", "YELLOW", "RED", name="dataqualitylevel")

    sa.Table(
        "user",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("feishu_open_id", sa.String(128), unique=True),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "user_risk_profile",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), index=True, nullable=False),
        sa.Column("questionnaire_version", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("raw_answers", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "disclaimer_acceptance",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), index=True, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "account",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), index=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("account_type", account_type_enum, nullable=False),
        sa.Column("base_currency", sa.String(8), nullable=False),
        sa.Column("available_cash", sa.Numeric(18, 4), nullable=False),
        sa.Column("frozen_cash", sa.Numeric(18, 4), nullable=False),
        sa.Column("in_transit_cash", sa.Numeric(18, 4), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "fund",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(20), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("board", sa.String(100), nullable=False),
        sa.Column("share_class", sa.String(8), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("risk_level", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("sales_platform", sa.String(100), nullable=False),
        sa.Column("trading_calendar", sa.String(32), nullable=False),
        sa.Column("cut_off_time", sa.Time(), nullable=False),
        sa.Column("confirm_days_buy", sa.Integer(), nullable=False),
        sa.Column("confirm_days_sell", sa.Integer(), nullable=False),
        sa.Column("subscription_open", sa.Boolean(), nullable=False),
        sa.Column("redemption_open", sa.Boolean(), nullable=False),
        sa.Column("purchase_limit", sa.Numeric(18, 4)),
        sa.Column("min_holding_days", sa.Integer(), nullable=False),
        sa.Column("fee_version", sa.String(32), nullable=False),
        sa.Column("fee_version_effective_at", sa.DateTime()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "fee_rule",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), index=True, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("fee_type", sa.String(32), nullable=False),
        sa.Column("min_days", sa.Integer()),
        sa.Column("max_days", sa.Integer()),
        sa.Column("rate", sa.Numeric(12, 8), nullable=False),
        sa.Column("fixed_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "holding_lot",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("account.id"), index=True, nullable=False),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), index=True, nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_nav", sa.Numeric(18, 8), nullable=False),
        sa.Column("total_shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("available_shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("frozen_shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("source_order_id", sa.String(36), index=True),
        *_timestamps(),
    )

    sa.Table(
        "cash_flow",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("account.id"), index=True, nullable=False),
        sa.Column("order_id", sa.String(36), index=True),
        sa.Column("flow_type", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("status", cash_status_enum, nullable=False),
        sa.Column("available_at", sa.DateTime()),
        sa.Column("note", sa.Text(), nullable=False),
        *_timestamps(),
    )

    orders = sa.Table(
        "orders",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("account.id"), index=True, nullable=False),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), index=True),
        sa.Column("convert_to_fund_id", sa.String(36), sa.ForeignKey("fund.id")),
        sa.Column("side", order_side_enum, nullable=False),
        sa.Column("status", order_status_enum, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4)),
        sa.Column("shares", sa.Numeric(18, 8)),
        sa.Column("ratio", sa.Numeric(12, 8)),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("emergency_exit", sa.Boolean(), nullable=False),
        sa.Column("emergency_approved_by", sa.String(36)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("data_snapshot", sa.JSON(), nullable=False),
        sa.Column("risk_snapshot", sa.JSON(), nullable=False),
        sa.Column("session_id", sa.String(36), index=True),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("confirmed_at", sa.DateTime()),
        *_timestamps(),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_account_idempotency_key"),
    )
    sa.Index("ix_order_status_expiry", orders.c.status, orders.c.expires_at)

    sa.Table(
        "order_version",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), index=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("changed_by", sa.String(36)),
        sa.Column("change_reason", sa.Text(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("order_id", "version", name="uq_order_version"),
    )

    sa.Table(
        "order_event",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), index=True, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(64), nullable=False),
        sa.Column("to_status", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36)),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "order_leg",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), index=True, nullable=False),
        sa.Column("leg_no", sa.Integer(), nullable=False),
        sa.Column("leg_type", sa.String(16), nullable=False),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expected_amount", sa.Numeric(18, 4)),
        sa.Column("expected_shares", sa.Numeric(18, 8)),
        sa.Column("confirmed_amount", sa.Numeric(18, 4)),
        sa.Column("confirmed_shares", sa.Numeric(18, 8)),
        *_timestamps(),
    )

    sa.Table(
        "trade_fill",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("orders.id"), index=True, nullable=False),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), nullable=False),
        sa.Column("confirmed_nav", sa.Numeric(18, 8), nullable=False),
        sa.Column("gross_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("net_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("shares", sa.Numeric(18, 8), nullable=False),
        sa.Column("fee_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.Column("confirmation_ref", sa.String(128), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "nav_confirm",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fund_id", sa.String(36), sa.ForeignKey("fund.id"), index=True, nullable=False),
        sa.Column("nav_date", sa.Date(), nullable=False),
        sa.Column("nav", sa.Numeric(18, 8), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("fund_id", "nav_date", name="uq_fund_nav_date"),
    )

    sa.Table(
        "reconciliation",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("account.id"), index=True, nullable=False),
        sa.Column("reconcile_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("status", reconciliation_status_enum, nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "reconciliation_diff",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "reconciliation_id",
            sa.String(36),
            sa.ForeignKey("reconciliation.id"),
            index=True,
            nullable=False,
        ),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("expected", sa.Text(), nullable=False),
        sa.Column("actual", sa.Text(), nullable=False),
        sa.Column("tolerance", sa.String(64), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "data_source",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("sla_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "data_quality",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(64), index=True, nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("level", data_quality_enum, nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "prompt_version",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("role_name", sa.String(64), index=True, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "model_call_log",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("role_name", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("estimated_cost", sa.Numeric(18, 8)),
        sa.Column("error", sa.Text(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "session",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("feishu_open_id", sa.String(128), index=True, nullable=False),
        sa.Column("chat_id", sa.String(128), index=True),
        sa.Column("current_order_id", sa.String(36)),
        sa.Column("current_order_version", sa.Integer()),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        *_timestamps(),
    )

    sa.Table(
        "notification_log",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("recipient", sa.String(128), nullable=False),
        sa.Column("message_type", sa.String(64), nullable=False),
        sa.Column("external_message_id", sa.String(128)),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "risk_rule",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(64), unique=True, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("rule_type", sa.String(16), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "trading_calendar",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("calendar", sa.String(32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(200), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("calendar", "trade_date", name="uq_calendar_date"),
    )

    sa.Table(
        "audit_log",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128)),
        sa.Column("action", sa.String(128), index=True, nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(128)),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
    )

    sa.Table(
        "portfolio_snapshot",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("account.id"), index=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), index=True, nullable=False),
        sa.Column("confirmed_assets", sa.Numeric(18, 4), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("cumulative_pnl", sa.Numeric(18, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "snapshot_date", name="uq_account_snapshot_date"),
    )

    sa.Table(
        "feishu_callback",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(128), unique=True, index=True, nullable=False),
        sa.Column("nonce", sa.String(128), index=True, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default=sa.text("200")),
    )

    sa.Table(
        "api_credential",
        md,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("user.id"), index=True, nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )

    return md


def upgrade() -> None:
    bind = op.get_bind()
    baseline = _baseline_metadata()

    # checkfirst=True makes this safe for both a fresh database and an existing
    # pre-Alembic V1.2.1 database. Existing tables are not rewritten.
    baseline.create_all(bind=bind, checkfirst=True)

    inspector = sa.inspect(bind)
    callback_columns = {column["name"] for column in inspector.get_columns("feishu_callback")}
    if "status_code" not in callback_columns:
        op.add_column(
            "feishu_callback",
            sa.Column("status_code", sa.Integer(), nullable=False, server_default=sa.text("200")),
        )


def downgrade() -> None:
    # This is an adoption baseline: the pre-Alembic application schema already
    # existed before revision tracking. Downgrading therefore removes only the
    # V1.2.2 delta and intentionally leaves the baseline tables in place.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("feishu_callback"):
        callback_columns = {column["name"] for column in inspector.get_columns("feishu_callback")}
        if "status_code" in callback_columns:
            op.drop_column("feishu_callback", "status_code")
