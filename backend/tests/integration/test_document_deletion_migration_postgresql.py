"""Exercise permanent-deletion schema changes on a disposable PostgreSQL database."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, column, create_engine, select, table, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.documents.deletion_contracts import DocumentDeletionState
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentDeletionRow,
    DocumentRow,
    SqlAlchemyDocumentRepository,
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
from tests.integration.factories import FIXTURE_TIME, make_research_source, persist_research_source

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_DELETION_REVISION = "0009_m1_5_reranking_operations"
DELETION_REVISION = "0011_document_purge_guards"


def test_deletion_migration_preserves_sources_costs_and_irreversible_receipts(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never touch the normal archive or erase deletion history during a downgrade."""

    database_name = f"investment_research_deletion_migration_{uuid4().hex[:16]}"
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
            "INVESTMENT_RESEARCH_DATABASE_URL", migration_url.render_as_string(hide_password=False)
        )
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        command.upgrade(config, PRE_DELETION_REVISION)
        migration_engine = create_engine(migration_url, hide_parameters=True)
        session_factory = create_session_factory(migration_engine)
        source = make_research_source(label="Deletion migration synthetic source")
        persist_research_source(migration_engine, source)
        operation = ProviderOperation(
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
                legacy_operations.insert().values(**operation.model_dump(exclude={"owner_deleted"}))
            )

        # Migration itself is additive: it never purges existing sources or resets accounting.
        command.upgrade(config, DELETION_REVISION)
        # Metadata comparison must use the latest schema, not the historical deletion revision.
        command.upgrade(config, "head")
        command.check(config)
        with session_factory() as session:
            assert (
                SqlAlchemyDocumentRepository(session).get_document_by_id(
                    source.context, source.document.id
                )
                == source.document
            )
            assert (
                SqlAlchemyProviderOperationRepository(session).get_by_id(
                    source.context, operation.id
                )
                == operation
            )
            assert session.scalar(select(DocumentDeletionRow.document_id)) is None
        command.downgrade(config, PRE_DELETION_REVISION)
        command.upgrade(config, DELETION_REVISION)

        # Normal accounting still requires exactly one correctly scoped owner.
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.execute(
                update(ProviderOperationRow)
                .where(ProviderOperationRow.id == operation.id)
                .values(research_request_id=None)
            )
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.execute(
                update(ProviderOperationRow)
                .where(ProviderOperationRow.id == operation.id)
                .values(owner_deleted=True)
            )
        with transactional_session(session_factory) as session:
            session.execute(
                update(ProviderOperationRow)
                .where(ProviderOperationRow.id == operation.id)
                .values(research_request_id=None, owner_deleted=True)
            )
        with session_factory() as session:
            detached = SqlAlchemyProviderOperationRepository(session).get_by_id(
                source.context, operation.id
            )
            assert detached is not None and detached.owner_deleted
            assert detached.reserved_cost_micro_usd == operation.reserved_cost_micro_usd

        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.add(
                DocumentDeletionRow(
                    workspace_id=uuid4(),
                    document_id=source.document.id,
                    state=DocumentDeletionState.PENDING,
                    requested_at=FIXTURE_TIME,
                )
            )
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.add(
                DocumentDeletionRow(
                    workspace_id=source.context.workspace_id,
                    document_id=source.document.id,
                    state=DocumentDeletionState.COMPLETED,
                    requested_at=FIXTURE_TIME,
                )
            )
        with transactional_session(session_factory) as session:
            session.add(
                DocumentDeletionRow(
                    workspace_id=source.context.workspace_id,
                    document_id=source.document.id,
                    state=DocumentDeletionState.PENDING,
                    requested_at=FIXTURE_TIME,
                )
            )
            session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.workspace_id == source.context.workspace_id,
                    DocumentRow.id == source.document.id,
                )
                .values(lifecycle_state=DocumentLifecycleState.PURGE_PENDING)
            )
        with session_factory() as session:
            assert (
                SqlAlchemyDocumentRepository(session).get_document_by_id(
                    source.context, source.document.id
                )
                is None
            )
        with pytest.raises(IntegrityError), transactional_session(session_factory) as session:
            session.add(
                DocumentDeletionRow(
                    workspace_id=source.context.workspace_id,
                    document_id=source.document.id,
                    state=DocumentDeletionState.PENDING,
                    requested_at=FIXTURE_TIME,
                )
            )

        with pytest.raises(DBAPIError):
            command.downgrade(config, PRE_DELETION_REVISION)
        with migration_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == DELETION_REVISION
            )
        with session_factory() as session:
            assert (
                session.scalar(select(DocumentDeletionRow.state)) is DocumentDeletionState.PENDING
            )
            assert (
                session.scalar(
                    select(ProviderOperationRow.reserved_cost_micro_usd).where(
                        ProviderOperationRow.id == operation.id
                    )
                )
                == 20
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
