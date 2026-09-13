from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args=connect_args,
)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def expected_schema_heads() -> set[str]:
    """Return the Alembic heads shipped with this application build."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return set(ScriptDirectory.from_config(config).get_heads())


def verify_schema_current() -> None:
    """Fail fast when the database has not been migrated to this build's head.

    Schema creation/upgrades are intentionally not performed at application startup.
    Operators must run `alembic upgrade head` before starting the service.
    """
    expected = expected_schema_heads()
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            raise RuntimeError(
                "database is not Alembic-managed; run `alembic upgrade head` before startup"
            )
        current = {
            row[0]
            for row in connection.execute(text("SELECT version_num FROM alembic_version"))
        }
    if current != expected:
        raise RuntimeError(
            "database schema revision mismatch: "
            f"current={sorted(current)!r} expected={sorted(expected)!r}; "
            "run `alembic upgrade head`"
        )
