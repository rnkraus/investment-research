"""Preserve archive history and refuse company loss during schema rollback."""

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from investment_research.modules.companies.persistence import (
    CompanyRow,
    SqlAlchemyCompanyRepository,
)
from investment_research.modules.documents.persistence import (
    DocumentCompanyRevisionRow,
    DocumentCompanyRow,
    DocumentRow,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ProcessingRunRow
from investment_research.settings import DatabaseSettings
from tests.integration.test_company_postgresql import make_company
from tests.integration.test_processing_outbox_migration_postgresql import seed_legacy_run

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def company_migration_database(
    integration_database_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Engine, Config]]:
    """Use a separate disposable database so rollback tests cannot alter other fixtures."""

    database_name = f"investment_research_company_migration_{uuid4().hex[:12]}"
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
        command.upgrade(config, "0014_outbox_exhaustion_receipts")
        engine = create_engine(url, hide_parameters=True)
        yield engine, config
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()


def test_company_migration_preserves_archive_and_allows_empty_rollback(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "head")
    command.check(config)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentCompanyRow)) == 0
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document
        run = session.get(ProcessingRunRow, source.processing_run.id)
        assert run is not None and run.to_domain() == source.processing_run

    command.downgrade(config, "0014_outbox_exhaustion_receipts")
    assert "document_companies" not in inspect(engine).get_table_names()
    assert "companies" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document


def test_company_link_migration_refuses_to_discard_assignments(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "head")
    company = make_company(source.context)
    key = (source.context.workspace_id, source.document.id, company.id)
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(source.context, company)
        session.flush()
        session.add(
            DocumentCompanyRow(
                workspace_id=key[0],
                document_id=key[1],
                company_id=key[2],
                reviewed_by=source.context.user_id,
                reviewed_at=company.created_at,
            )
        )

    with pytest.raises(RuntimeError, match="document-company links exist"):
        command.downgrade(config, "0015_company_identity")

    with Session(engine) as session:
        assert session.get(DocumentCompanyRow, key) is not None


def test_company_migration_refuses_to_discard_company_records(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "head")
    company = make_company(source.context)
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(source.context, company)

    with pytest.raises(RuntimeError, match="company records exist"):
        command.downgrade(config, "0014_outbox_exhaustion_receipts")

    with Session(engine) as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
            ScriptDirectory.from_config(config).get_current_head()
        )
        assert SqlAlchemyCompanyRepository(session).get_by_id(source.context, company.id) == company
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document
    command.check(config)


def test_company_revision_migration_retains_revision_for_an_empty_set(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "head")
    key = (source.context.workspace_id, source.document.id)
    with Session(engine) as session, session.begin():
        session.add(DocumentCompanyRevisionRow(workspace_id=key[0], document_id=key[1], revision=2))

    with pytest.raises(RuntimeError, match="company assignment revisions exist"):
        command.downgrade(config, "0016_document_companies")

    with Session(engine) as session:
        revision = session.get(DocumentCompanyRevisionRow, key)
        assert revision is not None and revision.revision == 2


def test_ticker_migration_preserves_existing_company_and_guards_rollback(
    company_migration_database: tuple[Engine, Config],
) -> None:
    engine, config = company_migration_database
    source = seed_legacy_run(engine, ProcessingState.CAPTURED)
    command.upgrade(config, "0017_company_link_revision")
    company = make_company(source.context)
    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO companies
                    (id, workspace_id, name, normalized_name, created_by, created_at)
                VALUES (:id, :workspace_id, :name, :normalized_name, :created_by, :created_at)
            """),
            {**company.model_dump(exclude={"ticker"}), "normalized_name": company.normalized_name},
        )
    command.upgrade(config, "head")
    with Session(engine) as session, session.begin():
        repository = SqlAlchemyCompanyRepository(session)
        assert repository.get_by_id(source.context, company.id) == company
        repository.update_ticker(source.context, company.id, "NVDA")
    with pytest.raises(RuntimeError, match="company tickers exist"):
        command.downgrade(config, "0017_company_link_revision")
    with Session(engine) as session:
        stored = SqlAlchemyCompanyRepository(session).get_by_id(source.context, company.id)
        assert stored is not None and stored.ticker == "NVDA"
