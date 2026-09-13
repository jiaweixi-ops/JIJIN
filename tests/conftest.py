from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import (  # noqa: F401
    hardening_models,
    models,
    operational_models,
    security_models,
    snapshot_models,
)
from app.db import Base


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    with Session() as session:
        yield session
