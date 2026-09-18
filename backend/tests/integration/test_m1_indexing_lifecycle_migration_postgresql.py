"""PostgreSQL migration regression for the M1 indexing lifecycle."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    FIXTURE_TEXTS,
    make_searchable_research_source,
    persist_research_source,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_LIFECYCLE_REVISION = "0005_m1_indexing_config"
INDEXING_LIFECYCLE_REVISION = "0006_m1_indexing_lifecycle"
M1_LEXICAL_CONFIG_VERSION = "m1-english-lexical-v1"
M1_EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
M1_DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


def _alembic_config() -> Config:
    """Load the repository migration configuration for an isolated database."""

    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _read_processing_run(engine: Engine, *, processing_run_id: UUID) -> dict[str, object]:
    """Read lifecycle fields that must survive the constraint-only migration."""

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT state, completed_at, embedding_config_version, "
                    "dense_index_config_version FROM processing_runs "
                    "WHERE id = :processing_run_id"
                ),
                {"processing_run_id": processing_run_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_lifecycle_migration_preserves_historical_terminal_m1_run(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept the old M1 terminal shape while enabling new resumable runs."""

    database_name = f"investment_research_migration_{uuid4().hex}"
    base_url = make_url(integration_database_settings.require_database_url())
    admin_engine = create_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    migration_engine: Engine | None = None
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        migration_url = base_url.set(database=database_name)
        monkeypatch.setenv(
            "INVESTMENT_RESEARCH_DATABASE_URL",
            migration_url.render_as_string(hide_password=False),
        )
        config = _alembic_config()
        command.upgrade(config, PRE_LIFECYCLE_REVISION)
        migration_engine = create_engine(migration_url, hide_parameters=True)

        source = make_searchable_research_source(
            label="M1 lifecycle migration regression",
            primary_text=FIXTURE_TEXTS[0],
            lexical_config_version=M1_LEXICAL_CONFIG_VERSION,
        )
        source = replace(
            source,
            processing_run=source.processing_run.model_copy(
                update={
                    "embedding_config_version": M1_EMBEDDING_CONFIG_VERSION,
                    "dense_index_config_version": M1_DENSE_INDEX_CONFIG_VERSION,
                }
            ),
        )
        persist_research_source(migration_engine, source)
        before = _read_processing_run(
            migration_engine,
            processing_run_id=source.processing_run.id,
        )

        migration_engine.dispose()
        command.upgrade(config, INDEXING_LIFECYCLE_REVISION)

        assert (
            _read_processing_run(
                migration_engine,
                processing_run_id=source.processing_run.id,
            )
            == before
        )
        assert before["state"] == "LEXICAL_READY"
        assert before["completed_at"] is not None
    finally:
        if migration_engine is not None:
            migration_engine.dispose()
        try:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        finally:
            admin_engine.dispose()
