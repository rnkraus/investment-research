"""Unit tests for SQLAlchemy engine, session, and transaction management."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Column, Engine, Integer, MetaData, Table, create_engine, func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.pool import StaticPool

from investment_research.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from investment_research.settings import Settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


@pytest.fixture
def temporary_database() -> Iterator[tuple[Engine, Table]]:
    """Provide one shared in-memory database and dispose it after each test."""

    metadata = MetaData()
    records = Table("records", metadata, Column("id", Integer, primary_key=True))
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    metadata.create_all(engine)

    try:
        yield engine, records
    finally:
        engine.dispose()


def test_create_database_engine_uses_safe_postgresql_configuration() -> None:
    settings = Settings.model_validate({"database_url": LOCAL_DATABASE_URL})

    engine = create_database_engine(settings)

    try:
        rendered_url = engine.url.render_as_string()
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.hide_parameters is True
        assert "test-password" not in rendered_url
        assert "***" in rendered_url
    finally:
        engine.dispose()


def test_transactional_session_commits_on_success(
    temporary_database: tuple[Engine, Table],
) -> None:
    engine, records = temporary_database
    session_factory = create_session_factory(engine)

    with transactional_session(session_factory) as session:
        session.execute(records.insert().values(id=1))

    with engine.connect() as connection:
        record_count = connection.scalar(select(func.count()).select_from(records))

    assert record_count == 1


def test_transactional_session_rolls_back_on_error(
    temporary_database: tuple[Engine, Table],
) -> None:
    engine, records = temporary_database
    session_factory = create_session_factory(engine)

    with (
        pytest.raises(RuntimeError, match="force rollback"),
        transactional_session(session_factory) as session,
    ):
        session.execute(records.insert().values(id=1))
        raise RuntimeError("force rollback")

    with engine.connect() as connection:
        record_count = connection.scalar(select(func.count()).select_from(records))

    assert record_count == 0


def test_session_factory_creates_separate_non_reusable_sessions(
    temporary_database: tuple[Engine, Table],
) -> None:
    engine, _ = temporary_database
    session_factory = create_session_factory(engine)
    first_session = session_factory()
    second_session = session_factory()

    assert first_session is not second_session

    first_session.close()
    second_session.close()

    with pytest.raises(InvalidRequestError, match="permanently closed"):
        first_session.execute(select(1))
