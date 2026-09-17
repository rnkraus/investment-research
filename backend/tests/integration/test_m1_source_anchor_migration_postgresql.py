"""PostgreSQL migration regression for stable M0 PDF source anchors."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.domain import EvidenceSpan
from investment_research.modules.evidence.persistence import SqlAlchemyEvidenceRepository
from investment_research.settings import DatabaseSettings
from tests.integration.factories import (
    FIXTURE_TIME,
    make_research_source,
    persist_research_source,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PRE_M1_REVISION = "0003_processing_execution_lease"


def _alembic_config() -> Config:
    """Load the repository migration configuration for the isolated test database."""

    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _read_pdf_anchor(
    engine: Engine,
    *,
    block_id: object,
    span_id: object,
    include_locator: bool,
) -> dict[str, object]:
    """Read only the immutable PDF anchor fields available at the selected revision."""

    locator_columns = (
        ", sb.source_locator_kind AS block_locator_kind, "
        "es.source_locator_kind AS span_locator_kind, "
        "es.source_start_character, es.source_end_character"
        if include_locator
        else ""
    )
    statement = text(
        "SELECT sb.id AS block_id, sb.page_number AS block_page_number, "
        "sb.start_character AS block_start_character, "
        "sb.end_character AS block_end_character, sb.text AS block_text, "
        "es.id AS span_id, es.page_number AS span_page_number, "
        "es.start_character AS span_start_character, "
        "es.end_character AS span_end_character, es.exact_text, es.text_sha256"
        f"{locator_columns} "
        "FROM structure_blocks AS sb "
        "JOIN evidence_spans AS es ON es.structure_block_id = sb.id "
        "WHERE sb.id = :block_id AND es.id = :span_id"
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                statement,
                {"block_id": block_id, "span_id": span_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_m1_migration_preserves_m0_pdf_anchor_identity(
    integration_database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backfill the discriminator without changing any historical PDF citation value."""

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
            # The identifier is safe to interpolate because it contains only a fixed
            # prefix and UUID hexadecimal characters generated inside this test.
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        migration_url = base_url.set(database=database_name)
        monkeypatch.setenv(
            "INVESTMENT_RESEARCH_DATABASE_URL",
            migration_url.render_as_string(hide_password=False),
        )
        config = _alembic_config()
        command.upgrade(config, "head")
        migration_engine = create_engine(migration_url, hide_parameters=True)

        source = make_research_source(label="M1 migration regression")
        persist_research_source(migration_engine, source)
        block = source.blocks[0]
        span = EvidenceSpan(
            id=uuid4(),
            workspace_id=source.context.workspace_id,
            document_version_id=source.version.id,
            structure_block_id=block.id,
            source_locator_kind=SourceLocatorKind.PDF_PAGE,
            page_number=block.page_number,
            source_start_character=None,
            source_end_character=None,
            start_character=0,
            end_character=len(block.text),
            exact_text=block.text,
            text_sha256=sha256(block.text.encode("utf-8")).hexdigest(),
            created_at=FIXTURE_TIME,
        )
        session_factory = create_session_factory(migration_engine)
        with transactional_session(session_factory) as session:
            SqlAlchemyEvidenceRepository(session).add_span(source.context, span)

        expected_anchor = {
            "block_id": block.id,
            "block_page_number": block.page_number,
            "block_start_character": block.start_character,
            "block_end_character": block.end_character,
            "block_text": block.text,
            "span_id": span.id,
            "span_page_number": span.page_number,
            "span_start_character": span.start_character,
            "span_end_character": span.end_character,
            "exact_text": span.exact_text,
            "text_sha256": span.text_sha256,
        }

        migration_engine.dispose()
        command.downgrade(config, PRE_M1_REVISION)
        assert (
            _read_pdf_anchor(
                migration_engine,
                block_id=block.id,
                span_id=span.id,
                include_locator=False,
            )
            == expected_anchor
        )

        migration_engine.dispose()
        command.upgrade(config, "head")
        upgraded_anchor = _read_pdf_anchor(
            migration_engine,
            block_id=block.id,
            span_id=span.id,
            include_locator=True,
        )
        assert upgraded_anchor == {
            **expected_anchor,
            "block_locator_kind": SourceLocatorKind.PDF_PAGE.value,
            "span_locator_kind": SourceLocatorKind.PDF_PAGE.value,
            "source_start_character": None,
            "source_end_character": None,
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
