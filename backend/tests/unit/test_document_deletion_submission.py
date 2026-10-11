"""Prove post-commit publication, bounded retries by resubmission, and scoped reads."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest

from investment_research.modules.documents.deletion import (
    GetDocumentDeletionService,
    RequestDocumentDeletionService,
    SubmitDocumentDeletionService,
)
from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
)
from investment_research.modules.documents.deletion_ports import (
    DocumentDeletionRepository,
    DocumentDeletionUnavailableError,
    DocumentDeletionUnitOfWork,
    DocumentPurgeDispatcher,
    DocumentPurgeDispatchError,
)
from investment_research.modules.identity.domain import WorkspaceContext

CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))
NOW = datetime(2026, 10, 12, tzinfo=UTC)


def receipt(number: int, *, completed: bool = False) -> DocumentDeletionReceipt:
    return DocumentDeletionReceipt(
        workspace_id=CONTEXT.workspace_id,
        document_id=UUID(int=number),
        state=DocumentDeletionState.COMPLETED if completed else DocumentDeletionState.PENDING,
        requested_at=NOW,
        completed_at=NOW if completed else None,
    )


def test_submission_commits_before_dispatch_and_continues_after_a_broker_failure() -> None:
    request = create_autospec(RequestDocumentDeletionService, instance=True)
    dispatcher = create_autospec(DocumentPurgeDispatcher, instance=True)
    pending = (receipt(3), receipt(4))
    completed = receipt(5, completed=True)
    request.request.return_value = (*pending, completed)
    dispatcher.dispatch.side_effect = [DocumentPurgeDispatchError("private broker detail"), None]
    command = DeleteDocumentsCommand(
        document_ids=tuple(item.document_id for item in (*pending, completed))
    )
    service = SubmitDocumentDeletionService(request, dispatcher)

    result = service.submit(CONTEXT, command)

    request.request.assert_called_once_with(CONTEXT, command)
    assert result.receipts == (*pending, completed)
    assert result.dispatch_pending_document_ids == (pending[0].document_id,)
    assert [call.args[0].document_id for call in dispatcher.dispatch.call_args_list] == [
        item.document_id for item in pending
    ]
    assert all(
        call.args[0].workspace_id == CONTEXT.workspace_id
        for call in dispatcher.dispatch.call_args_list
    )
    dispatcher.dispatch.side_effect = None
    repeated = service.submit(CONTEXT, command)
    assert repeated.receipts == result.receipts
    assert repeated.dispatch_pending_document_ids == ()


def test_failed_admission_publishes_nothing_and_unexpected_dispatch_errors_are_visible() -> None:
    request = create_autospec(RequestDocumentDeletionService, instance=True)
    dispatcher = create_autospec(DocumentPurgeDispatcher, instance=True)
    command = DeleteDocumentsCommand(document_ids=(UUID(int=3),))
    service = SubmitDocumentDeletionService(request, dispatcher)
    request.request.side_effect = DocumentDeletionUnavailableError("unavailable")
    with pytest.raises(DocumentDeletionUnavailableError):
        service.submit(CONTEXT, command)
    dispatcher.dispatch.assert_not_called()
    request.request.side_effect = None
    request.request.return_value = (receipt(3),)
    dispatcher.dispatch.side_effect = TypeError("programming error")
    with pytest.raises(TypeError, match="programming error"):
        service.submit(CONTEXT, command)


def test_real_admission_service_closes_transaction_before_publication() -> None:
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    repository.request.return_value = (receipt(3),)
    events: list[str] = []

    class Repositories:
        deletions = repository

    @contextmanager
    def factory() -> Iterator[DocumentDeletionUnitOfWork]:
        events.append("transaction-start")
        yield cast(DocumentDeletionUnitOfWork, Repositories())
        events.append("transaction-commit")

    dispatcher = create_autospec(DocumentPurgeDispatcher, instance=True)
    dispatcher.dispatch.side_effect = lambda _: events.append("dispatch")
    SubmitDocumentDeletionService(RequestDocumentDeletionService(factory), dispatcher).submit(
        CONTEXT, DeleteDocumentsCommand(document_ids=(UUID(int=3),))
    )
    assert events == ["transaction-start", "transaction-commit", "dispatch"]


@pytest.mark.parametrize("exists", [True, False])
def test_status_read_scopes_the_receipt_and_closes_before_return(exists: bool) -> None:
    repository = create_autospec(DocumentDeletionRepository, instance=True)
    expected = receipt(3, completed=True)
    repository.get_receipt.return_value = expected if exists else None
    events: list[str] = []

    class Repositories:
        deletions = repository

    @contextmanager
    def factory() -> Iterator[DocumentDeletionUnitOfWork]:
        yield cast(DocumentDeletionUnitOfWork, Repositories())
        events.append("closed")

    service = GetDocumentDeletionService(factory)
    if exists:
        assert service.get(CONTEXT, expected.document_id) == expected
    else:
        with pytest.raises(DocumentDeletionUnavailableError):
            service.get(CONTEXT, expected.document_id)
    repository.get_receipt.assert_called_once_with(CONTEXT, expected.document_id)
    repository.request.assert_not_called()
    assert events == ["closed"]
