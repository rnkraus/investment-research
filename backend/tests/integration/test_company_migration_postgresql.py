"""Preserve archive history and refuse company loss during schema rollback."""

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from investment_research.modules.companies.persistence import (
    CompanyRow,
    SqlAlchemyCompanyRepository,
)
from investment_research.modules.documents.persistence import DocumentRow
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
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document
        run = session.get(ProcessingRunRow, source.processing_run.id)
        assert run is not None and run.to_domain() == source.processing_run

    command.downgrade(config, "0014_outbox_exhaustion_receipts")
    assert "companies" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CompanyRow)) == 0
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document


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
            "0015_company_identity"
        )
        assert SqlAlchemyCompanyRepository(session).get_by_id(source.context, company.id) == company
        document = session.get(DocumentRow, source.document.id)
        assert document is not None and document.to_domain() == source.document
    command.check(config)
