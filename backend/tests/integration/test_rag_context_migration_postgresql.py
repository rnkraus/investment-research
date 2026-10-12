"""Exercise context tuning upgrades and history-safe downgrades on a disposable database."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from investment_research.modules.search.persistence import RetrievalRunRow
from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_TUNING_REVISION = "0011_document_purge_guards"


def test_context_migration_preserves_history_and_guards_downgrade(
    integration_database_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_name = f"investment_research_rag_migration_{uuid4().hex[:16]}"
    base_url = make_url(integration_database_settings.require_database_url())
    admin_engine = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    engine: Engine | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = base_url.set(database=database_name)
        monkeypatch.setenv(
            "INVESTMENT_RESEARCH_DATABASE_URL", url.render_as_string(hide_password=False)
        )
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        command.upgrade(config, PRE_TUNING_REVISION)
        engine = create_engine(url, hide_parameters=True)
        source = make_research_source(label="RAG tuning migration")
        retrieval = make_retrieval_result(source)
        persist_research_source(engine, source)
        persist_retrieval_result(engine, source, retrieval)
        statement = (
            update(RetrievalRunRow)
            .where(RetrievalRunRow.id == retrieval.run.id)
            .values(
                mode="DUAL_LANE",
                embedding_config_version="synthetic-embedding-v1",
                dense_index_config_version="synthetic-index-v1",
                dense_candidate_limit=20,
                context_chunk_limit=32,
                context_character_limit=24_000,
            )
        )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(statement)

        command.upgrade(config, "head")
        command.check(config)
        with engine.connect() as connection:
            assert connection.scalar(select(RetrievalRunRow.query_text)) == retrieval.run.query_text
            assert connection.scalar(select(RetrievalRunRow.context_chunk_limit)) is None
        with engine.begin() as connection:
            connection.execute(statement)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(statement.values(context_chunk_limit=33))
        with pytest.raises(RuntimeError, match="history"):
            command.downgrade(config, PRE_TUNING_REVISION)
        with engine.connect() as connection:
            assert connection.scalar(select(RetrievalRunRow.context_chunk_limit)) == 32
        with engine.begin() as connection:
            connection.execute(statement.values(context_chunk_limit=8))
        command.downgrade(config, PRE_TUNING_REVISION)
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(statement)
        command.upgrade(config, "head")
        command.check(config)
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()
