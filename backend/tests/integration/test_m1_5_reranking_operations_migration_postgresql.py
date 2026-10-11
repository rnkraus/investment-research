"""Verify the additive reranking ledger migration on an isolated PostgreSQL database."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, column, create_engine, select, table, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationKind,
    ProviderOperationState,
)
from investment_research.modules.operations.persistence import (
    ProviderOperationRow,
    SqlAlchemyProviderOperationRepository,
)
from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    FIXTURE_TIME,
    make_research_source,
    persist_research_source,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_RERANKING_REVISION = "0008_m1_provider_operations"
CURRENT_REVISION = "0010_permanent_document_deletion"


def test_reranking_migration_preserves_history_and_enforces_owner_and_workspace(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the actual changed checks; Alembic check cannot compare their expressions."""

    database_name = f"investment_research_reranking_migration_{uuid4().hex[:16]}"
    base_url = make_url(integration_database_settings.require_database_url())
    admin_engine = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True
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
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        command.upgrade(config, PRE_RERANKING_REVISION)
        migration_engine = create_engine(migration_url, hide_parameters=True)
        session_factory = create_session_factory(migration_engine)
        source = make_research_source(label="Reranking migration source")
        foreign = make_research_source(label="Foreign reranking migration source")
        persist_research_source(migration_engine, source)
        persist_research_source(migration_engine, foreign)
        historical = ProviderOperation(
            id=uuid4(),
            workspace_id=source.context.workspace_id,
            correlation_id=uuid4(),
            research_request_id=source.request.id,
            operation=ProviderOperationKind.QUERY_EMBEDDING,
            provider="openai",
            model="text-embedding-3-small",
            configuration_version="m1-query-embedding-v1",
            state=ProviderOperationState.STARTED,
            reserved_cost_micro_usd=20,
            started_at=FIXTURE_TIME,
            trace_id=uuid4().hex,
        )
        reranking = ProviderOperation.model_validate(
            {
                **historical.model_dump(),
                "id": uuid4(),
                "operation": ProviderOperationKind.RERANKING,
                "provider": "pinecone",
                "model": "bge-reranker-v2-m3",
                "configuration_version": "m1-5-pinecone-bge-none-v1",
                "reserved_cost_micro_usd": 0,
            }
        )
        # The pre-deletion schema has no owner_deleted column. Use its historical shape
        # until upgrade, rather than selecting/inserting the current ORM's newer columns.
        legacy_operations = table(
            "provider_operations",
            *(
                column(item.name, item.type)
                for item in ProviderOperationRow.__table__.c
                if item.name != "owner_deleted"
            ),
        )
        with transactional_session(session_factory) as session:
            session.execute(
                legacy_operations.insert().values(
                    **historical.model_dump(exclude={"owner_deleted"})
                )
            )
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.execute(
                legacy_operations.insert().values(**reranking.model_dump(exclude={"owner_deleted"}))
            )

        # With only old-kind records, upgrade/downgrade/upgrade must preserve history exactly.
        command.upgrade(config, CURRENT_REVISION)
        command.downgrade(config, PRE_RERANKING_REVISION)
        command.upgrade(config, CURRENT_REVISION)
        with transactional_session(session_factory) as session:
            repository = SqlAlchemyProviderOperationRepository(session)
            assert repository.get_by_id(source.context, historical.id) == historical
            repository.add(source.context, reranking)
        completed = ProviderOperation.model_validate(
            {
                **reranking.model_dump(),
                "state": ProviderOperationState.SUCCEEDED,
                "actual_cost_micro_usd": 0,
                "input_units": 1,
                "output_units": 0,
                "completed_at": FIXTURE_TIME,
                "duration_ms": 0,
            }
        )
        with transactional_session(session_factory) as session:
            repository = SqlAlchemyProviderOperationRepository(session)
            assert repository.finalize(source.context, completed)
            assert repository.get_by_id(foreign.context, completed.id) is None
            assert repository.get_by_id(foreign.context, uuid4()) is None

        # Bypass application validation to prove PostgreSQL still enforces canonical ownership.
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.execute(
                update(ProviderOperationRow)
                .where(ProviderOperationRow.id == reranking.id)
                .values(research_request_id=None, processing_run_id=source.processing_run.id)
            )
        cross_workspace = ProviderOperation.model_validate(
            {
                **reranking.model_dump(),
                "id": uuid4(),
                "workspace_id": foreign.context.workspace_id,
            }
        )
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            SqlAlchemyProviderOperationRepository(session).add(foreign.context, cross_workspace)

        # A downgrade must fail atomically rather than delete or relabel a new-kind record.
        with pytest.raises(IntegrityError):
            command.downgrade(config, PRE_RERANKING_REVISION)
        with migration_engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == (CURRENT_REVISION)
        with transactional_session(session_factory) as session:
            repository = SqlAlchemyProviderOperationRepository(session)
            assert repository.get_by_id(source.context, completed.id) == completed
            assert repository.get_by_id(source.context, historical.id) == historical
            assert (
                session.scalar(
                    select(ProviderOperationRow.id).where(
                        ProviderOperationRow.id == cross_workspace.id
                    )
                )
                is None
            )
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
