"""PostgreSQL migration regression for immutable M1 indexing configuration."""

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
PRE_INDEXING_CONFIG_REVISION = "0004_m1_text_sources_and_anchors"
INDEXING_CONFIG_REVISION = "0005_m1_indexing_config"
M0_LEXICAL_CONFIG_VERSION = "m0.0-english-lexical-v1"
M0_EMBEDDING_CONFIG_VERSION = "m0.0-embedding-disabled-v1"
M0_DENSE_INDEX_CONFIG_VERSION = "m0.0-dense-index-disabled-v1"
M1_LEXICAL_CONFIG_VERSION = "m1-english-lexical-v1"
M1_EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
M1_DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


def _alembic_config() -> Config:
    """Load the repository migration configuration for an isolated database."""

    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _read_indexing_versions(
    engine: Engine,
    *,
    processing_run_id: UUID,
) -> dict[str, object]:
    """Read the immutable indexing configuration stored for one processing run."""

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT lexical_config_version, embedding_config_version, "
                    "dense_index_config_version "
                    "FROM processing_runs WHERE id = :processing_run_id"
                ),
                {"processing_run_id": processing_run_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _read_indexing_column_nullability(engine: Engine) -> dict[str, str]:
    """Return PostgreSQL nullability metadata for the new snapshot columns."""

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'processing_runs' "
                "AND column_name IN "
                "('embedding_config_version', 'dense_index_config_version')"
            )
        ).mappings()
    return {str(row["column_name"]): str(row["is_nullable"]) for row in rows}


def test_m1_indexing_config_migration_backfills_historical_runs(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backfill M0 and M1 runs without losing their lexical configuration."""

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
        command.upgrade(config, "head")
        migration_engine = create_engine(migration_url, hide_parameters=True)

        m0_source = make_searchable_research_source(
            label="M0 indexing migration regression",
            primary_text=FIXTURE_TEXTS[0],
            lexical_config_version=M0_LEXICAL_CONFIG_VERSION,
        )
        m1_source = make_searchable_research_source(
            label="M1 indexing migration regression",
            primary_text=FIXTURE_TEXTS[0],
            lexical_config_version=M1_LEXICAL_CONFIG_VERSION,
        )
        persist_research_source(migration_engine, m0_source)
        persist_research_source(migration_engine, m1_source)

        migration_engine.dispose()
        command.downgrade(config, PRE_INDEXING_CONFIG_REVISION)
        command.upgrade(config, INDEXING_CONFIG_REVISION)

        assert _read_indexing_versions(
            migration_engine,
            processing_run_id=m0_source.processing_run.id,
        ) == {
            "lexical_config_version": M0_LEXICAL_CONFIG_VERSION,
            "embedding_config_version": M0_EMBEDDING_CONFIG_VERSION,
            "dense_index_config_version": M0_DENSE_INDEX_CONFIG_VERSION,
        }
        assert _read_indexing_versions(
            migration_engine,
            processing_run_id=m1_source.processing_run.id,
        ) == {
            "lexical_config_version": M1_LEXICAL_CONFIG_VERSION,
            "embedding_config_version": M1_EMBEDDING_CONFIG_VERSION,
            "dense_index_config_version": M1_DENSE_INDEX_CONFIG_VERSION,
        }
        assert _read_indexing_column_nullability(migration_engine) == {
            "dense_index_config_version": "NO",
            "embedding_config_version": "NO",
        }
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
