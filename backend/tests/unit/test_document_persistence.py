"""Unit tests for immutable document domain values and scoped persistence."""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table
from sqlalchemy.orm import Session

from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentRelationshipError,
    DocumentScopeError,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
)
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    SqlAlchemyDocumentRepository,
)
from investment_research.modules.documents.ports import DocumentRepository
from investment_research.modules.identity.domain import WorkspaceContext

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
ORIGINAL_ID = UUID("10000000-0000-4000-8000-000000000003")
OTHER_ID = UUID("10000000-0000-4000-8000-000000000004")
CAPTURED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
CONTENT_SHA256 = "a" * 64


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build a deterministic trusted context for repository tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def make_capture(
    workspace_id: UUID = WORKSPACE_ID,
) -> tuple[Document, DocumentVersion, OriginalArtifact]:
    """Build one internally consistent synthetic document capture."""

    document = Document(
        id=DOCUMENT_ID,
        workspace_id=workspace_id,
        document_type="earnings_brief",
        lifecycle_state=DocumentLifecycleState.ACTIVE,
        current_version_id=VERSION_ID,
        created_at=CAPTURED_AT,
    )
    version = DocumentVersion(
        id=VERSION_ID,
        workspace_id=workspace_id,
        document_id=DOCUMENT_ID,
        version_number=1,
        title="Northstar Grid Analytics Ltd. - FY2025 Earnings Brief",
        language="en",
        content_sha256=CONTENT_SHA256,
        published_at=datetime(2026, 2, 19, tzinfo=UTC),
        available_at=datetime(2026, 2, 19, tzinfo=UTC),
        captured_at=CAPTURED_AT,
    )
    original = OriginalArtifact(
        id=ORIGINAL_ID,
        workspace_id=workspace_id,
        document_version_id=VERSION_ID,
        storage_key=f"workspaces/{workspace_id}/originals/{ORIGINAL_ID}.pdf",
        media_type=OriginalMediaType.PDF,
        byte_size=10_840,
        checksum_sha256=CONTENT_SHA256,
        captured_at=CAPTURED_AT,
    )
    return document, version, original


def test_original_artifact_rejects_unsafe_storage_key() -> None:
    _, _, original = make_capture()

    with pytest.raises(ValidationError, match="safe relative POSIX key"):
        OriginalArtifact.model_validate(
            {
                **original.model_dump(),
                "storage_key": "../private/original.pdf",
            }
        )


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: DocumentRepository = SqlAlchemyDocumentRepository(session)

    assert isinstance(repository, SqlAlchemyDocumentRepository)


def test_repository_stages_complete_capture_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyDocumentRepository(session)
    document, version, original = make_capture()

    repository.add_capture(make_context(), document, version, original)

    session.add_all.assert_called_once()
    document_row, version_row, original_row = session.add_all.call_args.args[0]
    assert isinstance(document_row, DocumentRow)
    assert isinstance(version_row, DocumentVersionRow)
    assert isinstance(original_row, OriginalArtifactRow)
    assert document_row.to_domain() == document
    assert version_row.to_domain() == version
    assert original_row.to_domain() == original


def test_repository_rejects_cross_workspace_capture_before_database_access() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyDocumentRepository(session)
    document, version, original = make_capture(OTHER_WORKSPACE_ID)

    with pytest.raises(DocumentScopeError, match="outside the active workspace"):
        repository.add_capture(make_context(), document, version, original)

    session.add_all.assert_not_called()


def test_repository_rejects_inconsistent_capture_relationships() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyDocumentRepository(session)
    document, version, original = make_capture()
    inconsistent_document = document.model_copy(update={"current_version_id": OTHER_ID})

    with pytest.raises(DocumentRelationshipError, match="relationships are inconsistent"):
        repository.add_capture(make_context(), inconsistent_document, version, original)

    session.add_all.assert_not_called()


def test_repository_returns_only_eligible_domain_objects() -> None:
    session = create_autospec(Session, instance=True)
    document, version, original = make_capture()
    session.scalar.side_effect = [
        DocumentRow.from_domain(document),
        DocumentVersionRow.from_domain(version),
        OriginalArtifactRow.from_domain(original),
    ]
    repository = SqlAlchemyDocumentRepository(session)

    loaded_document = repository.get_document_by_id(make_context(), DOCUMENT_ID)
    loaded_version = repository.get_current_version_by_id(make_context(), DOCUMENT_ID, VERSION_ID)
    loaded_original = repository.get_original_artifact(make_context(), DOCUMENT_ID, VERSION_ID)

    assert loaded_document == document
    assert loaded_version == version
    assert loaded_original == original
    assert session.scalar.call_count == 3


def test_repository_hides_unknown_or_ineligible_records() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    repository = SqlAlchemyDocumentRepository(session)

    assert repository.get_document_by_id(make_context(), OTHER_ID) is None
    assert repository.get_current_version_by_id(make_context(), DOCUMENT_ID, OTHER_ID) is None
    assert repository.get_original_artifact(make_context(), DOCUMENT_ID, OTHER_ID) is None


def test_document_tables_enforce_workspace_scoped_foreign_keys() -> None:
    document_table = cast(Table, DocumentRow.__table__)
    tables = (
        document_table,
        cast(Table, DocumentVersionRow.__table__),
        cast(Table, OriginalArtifactRow.__table__),
    )

    for table in tables:
        assert "workspace_id" in table.c
        target_sets = {
            tuple(element.target_fullname for element in constraint.elements)
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        }
        assert ("workspaces.id",) in target_sets

    current_version_constraint = next(
        constraint
        for constraint in document_table.constraints
        if constraint.name == "fk_documents_current_version"
    )
    assert isinstance(current_version_constraint, ForeignKeyConstraint)
    assert current_version_constraint.deferrable is True
    assert current_version_constraint.initially == "DEFERRED"
