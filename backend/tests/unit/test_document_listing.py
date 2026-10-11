"""Deterministic contracts, query, and lifecycle checks for the document catalog."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Select
from sqlalchemy.orm import Session

from investment_research.modules.documents.contracts import (
    DocumentListCursor,
    DocumentListEntry,
    DocumentListPage,
    DocumentListQuery,
)
from investment_research.modules.documents.domain import OriginalMediaType
from investment_research.modules.documents.listing import ListDocumentsService
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.documents.ports import DocumentReadUnitOfWork, DocumentRepository
from investment_research.modules.identity.domain import WorkspaceContext

CREATED_AT = datetime(2026, 10, 11, 12, tzinfo=UTC)
CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))


def make_entry(identifier: int) -> DocumentListEntry:
    """Build listing metadata without private storage or provider information."""

    return DocumentListEntry(
        document_id=UUID(int=identifier),
        document_version_id=UUID(int=identifier + 1000),
        title=f"Synthetic source {identifier}",
        language="en",
        media_type=OriginalMediaType.PDF,
        byte_size=128,
        created_at=CREATED_AT,
        captured_at=CREATED_AT,
    )


@pytest.mark.parametrize("page_size", [0, -1, 101, True, 1.5, "20"])
def test_document_list_query_rejects_unbounded_or_untyped_sizes(page_size: object) -> None:
    with pytest.raises(ValidationError):
        DocumentListQuery.model_validate({"page_size": page_size})


def test_document_list_query_rejects_client_workspace_and_naive_cursor() -> None:
    assert DocumentListQuery().page_size == 20
    assert DocumentListQuery(page_size=100).page_size == 100
    with pytest.raises(ValidationError):
        DocumentListQuery.model_validate({"workspace_id": str(CONTEXT.workspace_id)})
    with pytest.raises(ValidationError):
        DocumentListCursor(created_at=datetime(2026, 10, 11), document_id=UUID(int=3))


def test_document_list_cursor_normalizes_aware_time_to_utc() -> None:
    cursor = DocumentListCursor.model_validate(
        {"created_at": "2026-10-11T14:00:00+02:00", "document_id": str(UUID(int=3))}
    )

    assert cursor.created_at == CREATED_AT
    assert cursor.created_at.tzinfo is UTC


def test_document_list_entry_excludes_private_artifact_metadata() -> None:
    assert set(make_entry(3).model_dump()) == {
        "document_id",
        "document_version_id",
        "title",
        "language",
        "published_at",
        "available_at",
        "media_type",
        "byte_size",
        "created_at",
        "captured_at",
    }
    with pytest.raises(ValidationError):
        DocumentListEntry.model_validate({**make_entry(3).model_dump(), "storage_key": "private"})


@pytest.mark.parametrize("row_count", [0, 1, 2])
def test_document_repository_bounds_the_page_and_only_returns_a_cursor_for_more_rows(
    row_count: int,
) -> None:
    session = create_autospec(Session, instance=True)
    entries = tuple(make_entry(identifier) for identifier in (4, 3))[:row_count]
    session.execute.return_value.mappings.return_value.all.return_value = [
        entry.model_dump() for entry in entries
    ]

    page = SqlAlchemyDocumentRepository(session).list_current_documents(
        CONTEXT, DocumentListQuery(page_size=1)
    )

    assert page.items == entries[:1]
    assert page.next_cursor == (
        DocumentListCursor(created_at=CREATED_AT, document_id=entries[0].document_id)
        if row_count > 1
        else None
    )
    session.commit.assert_not_called()


def test_document_repository_query_is_scoped_current_and_keyset_ordered() -> None:
    session = create_autospec(Session, instance=True)
    session.execute.return_value.mappings.return_value.all.return_value = []
    cursor = DocumentListCursor(created_at=CREATED_AT, document_id=UUID(int=4))

    SqlAlchemyDocumentRepository(session).list_current_documents(
        CONTEXT, DocumentListQuery(page_size=5, after=cursor)
    )

    statement = cast(Select[tuple[object, ...]], session.execute.call_args.args[0])
    compiled = statement.compile()
    sql = str(compiled)
    assert "documents.workspace_id =" in sql
    assert "document_versions.workspace_id =" in sql
    assert "original_artifacts.workspace_id =" in sql
    assert "document_versions.id = documents.current_version_id" in sql
    assert "documents.lifecycle_state =" in sql
    assert "ORDER BY documents.created_at DESC, documents.id DESC" in sql
    assert "documents.created_at <" in sql
    assert "documents.id <" in sql
    assert "OFFSET" not in sql
    assert "storage_key" not in sql
    assert "checksum_sha256" not in sql
    assert list(compiled.params.values()).count(CONTEXT.workspace_id) == 3
    assert 6 in compiled.params.values()


@pytest.mark.parametrize("fails", [False, True])
def test_document_listing_service_propagates_scope_and_closes_its_read_session(fails: bool) -> None:
    repository_mock = create_autospec(DocumentRepository, instance=True)
    query = DocumentListQuery(page_size=5)
    expected = DocumentListPage(items=(make_entry(3),))
    repository_mock.list_current_documents.return_value = expected
    if fails:
        repository_mock.list_current_documents.side_effect = RuntimeError("synthetic read failure")
    lifecycle: list[str] = []

    class ReadUnitOfWork:
        documents = cast(DocumentRepository, repository_mock)

    @contextmanager
    def open_read() -> Iterator[DocumentReadUnitOfWork]:
        lifecycle.append("enter")
        try:
            yield ReadUnitOfWork()
        finally:
            lifecycle.append("exit")

    service = ListDocumentsService(unit_of_work_factory=open_read)
    if fails:
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            service.list_documents(CONTEXT, query)
    else:
        assert service.list_documents(CONTEXT, query) == expected

    assert lifecycle == ["enter", "exit"]
    repository_mock.list_current_documents.assert_called_once_with(CONTEXT, query)
