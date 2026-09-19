"""PostgreSQL migration regression for reproducible M1 retrieval selection."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
)
from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_RETRIEVAL_SELECTION_REVISION = "0006_m1_indexing_lifecycle"
RETRIEVAL_SELECTION_REVISION = "0007_m1_retrieval_selection"


def _alembic_config() -> Config:
    """Load the repository migration configuration for an isolated database."""

    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _read_retrieval_snapshot(
    engine: Engine,
    *,
    retrieval_run_id: UUID,
) -> dict[str, object]:
    """Read the M1 selection configuration stored for one historical run."""

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT mode, lexical_config_version, "
                    "embedding_config_version, dense_index_config_version, "
                    "selector_config_version, lexical_candidate_limit, "
                    "dense_candidate_limit, context_chunk_limit, "
                    "context_character_limit "
                    "FROM retrieval_runs WHERE id = :retrieval_run_id"
                ),
                {"retrieval_run_id": retrieval_run_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _read_selection_rank_nullability(engine: Engine) -> str:
    """Return PostgreSQL nullability metadata for the new candidate rank."""

    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'retrieval_candidates' "
                    "AND column_name = 'selection_rank'"
                )
            ).scalar_one()
        )


def test_m1_retrieval_selection_migration_backfills_historical_runs(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve M0 retrievals while adding explicit M1 selection metadata."""

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

        source = make_research_source(label="Retrieval selection migration regression")
        retrieval = make_retrieval_result(source)
        persist_research_source(migration_engine, source)
        persist_retrieval_result(migration_engine, source, retrieval)

        migration_engine.dispose()
        command.downgrade(config, PRE_RETRIEVAL_SELECTION_REVISION)
        command.upgrade(config, RETRIEVAL_SELECTION_REVISION)

        assert _read_retrieval_snapshot(
            migration_engine,
            retrieval_run_id=retrieval.run.id,
        ) == {
            "mode": "LEXICAL",
            "lexical_config_version": source.processing_run.lexical_config_version,
            "embedding_config_version": None,
            "dense_index_config_version": None,
            "selector_config_version": LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
            "lexical_candidate_limit": 10,
            "dense_candidate_limit": 0,
            "context_chunk_limit": None,
            "context_character_limit": None,
        }
        assert _read_selection_rank_nullability(migration_engine) == "YES"
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
