"""Upgrade inherited runs without reviving active, failed, terminal, or excluded work."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url

from investment_research.infrastructure.database.document_deletion import (
    PostgresDocumentDeletionRepository,
)
from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.documents.deletion_contracts import DeleteDocumentsCommand
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import (
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
)
from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_research_source,
    persist_workspace,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def seed_legacy_run(
    engine: Engine, state: ProcessingState, *, excluded: bool = False
) -> ResearchSource:
    source = make_research_source(label="Legacy outbox migration")
    values = source.processing_run.model_dump()
    if state is ProcessingState.CAPTURED:
        values.update(state=state, attempt_count=0, started_at=None, completed_at=None)
    elif state is ProcessingState.PARSING:
        values.update(
            state=state,
            completed_at=None,
            lease_id=uuid4(),
            lease_expires_at=FIXTURE_TIME + timedelta(seconds=360),
        )
    elif state is ProcessingState.FAILED:
        values.update(state=state, error_summary="Synthetic permanent failure")
    source = replace(source, processing_run=ProcessingRun.model_validate(values))
    persist_workspace(engine, source)
    with transactional_session(create_session_factory(engine)) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            source.context, source.document, source.version, source.original
        )
        SqlAlchemyProcessingRepository(session).add_run(source.context, source.processing_run)
    if excluded:
        with transactional_session(create_session_factory(engine)) as session:
            PostgresDocumentDeletionRepository(session).request(
                source.context,
                DeleteDocumentsCommand(document_ids=(source.document.id,)),
                FIXTURE_TIME + timedelta(days=1),
            )
    return source


def test_outbox_migration_backfills_only_unclaimed_active_capture_and_preserves_history(
    integration_database_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_name = f"investment_research_outbox_migration_{uuid4().hex[:16]}"
    base_url = make_url(integration_database_settings.require_database_url())
    admin = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    engine: Engine | None = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        url = base_url.set(database=database_name)
        monkeypatch.setenv(
            "INVESTMENT_RESEARCH_DATABASE_URL", url.render_as_string(hide_password=False)
        )
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        command.upgrade(config, "0012_rag_context_bounds")
        engine = create_engine(url, hide_parameters=True)
        eligible = seed_legacy_run(engine, ProcessingState.CAPTURED)
        sources = (
            eligible,
            seed_legacy_run(engine, ProcessingState.PARSING),
            seed_legacy_run(engine, ProcessingState.LEXICAL_READY),
            seed_legacy_run(engine, ProcessingState.FAILED),
            seed_legacy_run(engine, ProcessingState.CAPTURED, excluded=True),
        )
        command.upgrade(config, "head")
        command.check(config)
        with create_session_factory(engine)() as session:
            events = tuple(session.scalars(select(OutboxEventRow)))
            assert len(events) == 1
            assert events[0].to_domain() == OutboxEvent.for_processing(
                workspace_id=eligible.context.workspace_id,
                processing_run_id=eligible.processing_run.id,
                created_at=eligible.processing_run.created_at,
            )
            for source in sources:
                row = session.get(ProcessingRunRow, source.processing_run.id)
                assert row is not None
                assert row.to_domain() == source.processing_run
        with pytest.raises(RuntimeError, match="durable delivery intent"):
            command.downgrade(config, "0012_rag_context_bounds")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0013_processing_outbox"
            )
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
