"""PostgreSQL integration tests for atomic document capture."""

from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_capture_unit_of_work_factory,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.modules.documents.application import CaptureDocumentService
from investment_research.modules.documents.contracts import CaptureDocumentInput
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    SqlAlchemyDocumentRepository,
)
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import (
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
)

CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
PDF_CONTENT = b"%PDF-1.7\nsynthetic PostgreSQL capture test\n%%EOF\n"


def create_workspace(engine: Engine) -> WorkspaceContext:
    """Persist one unique synthetic workspace and return its trusted context."""

    workspace_id = uuid4()
    context = WorkspaceContext(user_id=uuid4(), workspace_id=workspace_id)
    workspace = Workspace(
        id=workspace_id,
        name=f"Capture Integration Workspace {workspace_id}",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=CAPTURED_AT,
    )
    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(context, workspace)
    return context


def make_capture_input() -> CaptureDocumentInput:
    """Return valid synthetic PDF metadata for the integration path."""

    return CaptureDocumentInput(
        filename="earnings-brief.pdf",
        media_type="application/pdf",
        title="Synthetic PostgreSQL Capture",
        language="en",
        published_at=datetime(2026, 2, 19, tzinfo=UTC),
        available_at=datetime(2026, 2, 19, tzinfo=UTC),
    )


def make_capture_service(
    engine: Engine,
    storage_root: Path,
    *,
    identifier_factory: Callable[[], UUID] = uuid4,
) -> CaptureDocumentService:
    """Build the real storage and transaction adapters around the application service."""

    session_factory = create_session_factory(engine)
    return CaptureDocumentService(
        object_storage=LocalObjectStorage(storage_root),
        unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
        max_upload_bytes=1024,
        clock=lambda: CAPTURED_AT,
        identifier_factory=identifier_factory,
    )


def test_capture_commits_document_original_and_processing_intent(
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    context = create_workspace(integration_database_engine)
    other_context = create_workspace(integration_database_engine)
    service = make_capture_service(integration_database_engine, tmp_path / "objects")

    result = service.capture(context, make_capture_input(), BytesIO(PDF_CONTENT))

    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        documents = SqlAlchemyDocumentRepository(session)
        processing = SqlAlchemyProcessingRepository(session)
        document = documents.get_document_by_id(context, result.document_id)
        version = documents.get_current_version_by_id(
            context,
            result.document_id,
            result.document_version_id,
        )
        original = documents.get_original_artifact(
            context,
            result.document_id,
            result.document_version_id,
        )
        processing_original = documents.get_original_artifact_by_version_id(
            context,
            result.document_version_id,
        )
        foreign_processing_original = documents.get_original_artifact_by_version_id(
            other_context,
            result.document_version_id,
        )
        unknown_processing_original = documents.get_original_artifact_by_version_id(
            context,
            uuid4(),
        )
        processing_run = processing.get_run_by_id(context, result.processing_run_id)

    assert document is not None
    assert version is not None
    assert original is not None
    assert processing_original == original
    assert foreign_processing_original is None
    assert unknown_processing_original is None
    assert processing_run is not None
    assert document.current_version_id == version.id
    assert original.document_version_id == version.id
    assert processing_run.document_version_id == version.id
    assert processing_run.state is ProcessingState.CAPTURED

    storage = LocalObjectStorage(tmp_path / "objects")
    with storage.open(context, original.storage_key) as stored_file:
        assert stored_file.read() == PDF_CONTENT


def test_capture_rolls_back_all_rows_and_discards_object_on_failure(
    integration_database_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = create_workspace(integration_database_engine)
    identifiers = tuple(uuid4() for _ in range(4))
    identifier_iterator = iter(identifiers)
    service = make_capture_service(
        integration_database_engine,
        tmp_path / "objects",
        identifier_factory=identifier_iterator.__next__,
    )

    def fail_processing_write(
        repository: SqlAlchemyProcessingRepository,
        operation_context: WorkspaceContext,
        run: ProcessingRun,
    ) -> None:
        del repository, operation_context, run
        raise RuntimeError("simulated processing persistence failure")

    monkeypatch.setattr(SqlAlchemyProcessingRepository, "add_run", fail_processing_write)

    with pytest.raises(RuntimeError, match="processing persistence failure"):
        service.capture(context, make_capture_input(), BytesIO(PDF_CONTENT))

    session_factory = create_session_factory(integration_database_engine)
    table_types = (DocumentRow, DocumentVersionRow, OriginalArtifactRow, ProcessingRunRow)
    with transactional_session(session_factory) as session:
        row_counts = tuple(
            session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.workspace_id == context.workspace_id)
            )
            for row_type in table_types
        )

    assert row_counts == (0, 0, 0, 0)
    assert list((tmp_path / "objects").rglob("*.pdf")) == []
