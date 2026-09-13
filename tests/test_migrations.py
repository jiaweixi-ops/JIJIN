from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]
HEAD = "20260913_02"


def _config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_alembic_upgrade_head_creates_fresh_sqlite_schema(tmp_path):
    database = tmp_path / "fresh.db"
    url = f"sqlite:///{database.as_posix()}"

    command.upgrade(_config(url), "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    assert "alembic_version" in inspector.get_table_names()
    assert "orders" in inspector.get_table_names()
    assert "feishu_callback" in inspector.get_table_names()
    assert "ai_usage_ledger" in inspector.get_table_names()
    assert "operational_run" in inspector.get_table_names()
    assert "research_inbox" in inspector.get_table_names()
    assert "research_evidence" in inspector.get_table_names()
    callback_columns = {column["name"] for column in inspector.get_columns("feishu_callback")}
    assert "status_code" in callback_columns
    credential_columns = {column["name"] for column in inspector.get_columns("api_credential")}
    assert {"hash_version", "expires_at", "revoked_at", "rotated_from_id"} <= credential_columns
    operational_columns = {column["name"] for column in inspector.get_columns("operational_run")}
    assert {"job_name", "business_date", "status", "attempt", "summary"} <= operational_columns
    research_columns = {column["name"] for column in inspector.get_columns("research_inbox")}
    assert {
        "account_id",
        "fund_id",
        "payload_hash",
        "status",
        "research_packet",
        "structured_packet",
        "decision_plan",
        "candidate_order_ids",
    } <= research_columns
    evidence_columns = {column["name"] for column in inspector.get_columns("research_evidence")}
    assert {"research_item_id", "evidence_id", "source_url", "confidence"} <= evidence_columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD


def test_alembic_adopts_legacy_schema_and_backfills_callback_status(tmp_path):
    database = tmp_path / "legacy.db"
    url = f"sqlite:///{database.as_posix()}"
    engine = create_engine(url)

    # Minimal pre-Alembic V1.2.1 callback table: no status_code column.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE feishu_callback (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                event_id VARCHAR(128) NOT NULL UNIQUE,
                nonce VARCHAR(128) NOT NULL,
                received_at DATETIME NOT NULL,
                response JSON NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO feishu_callback (id, event_id, nonce, received_at, response)
            VALUES ('legacy-id', 'legacy-event', 'legacy-nonce', '2026-01-01 00:00:00', '{}')
            """
        )

    command.upgrade(_config(url), "head")

    inspector = inspect(engine)
    callback_columns = {column["name"] for column in inspector.get_columns("feishu_callback")}
    assert "status_code" in callback_columns
    assert "orders" in inspector.get_table_names()
    assert "ai_usage_ledger" in inspector.get_table_names()
    assert "operational_run" in inspector.get_table_names()
    assert "research_inbox" in inspector.get_table_names()
    assert "research_evidence" in inspector.get_table_names()
    with engine.connect() as connection:
        status_code = connection.execute(
            text("SELECT status_code FROM feishu_callback WHERE event_id='legacy-event'")
        ).scalar_one()
        assert status_code == 200
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD
