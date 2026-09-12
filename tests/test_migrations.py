from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


ROOT = Path(__file__).resolve().parents[1]


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
    callback_columns = {column["name"] for column in inspector.get_columns("feishu_callback")}
    assert "status_code" in callback_columns
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260912_01"


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
    assert "orders" in inspector.get_table_names()  # missing baseline tables are created
    with engine.connect() as connection:
        status_code = connection.execute(
            text("SELECT status_code FROM feishu_callback WHERE event_id='legacy-event'")
        ).scalar_one()
        assert status_code == 200
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260912_01"
