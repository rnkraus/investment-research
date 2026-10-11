"""Verify explicit bounded targets and content-free irreversible-deletion receipts."""

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import ForeignKeyConstraint, Table

from investment_research.modules.documents.deletion_contracts import (
    DOCUMENT_DELETION_LIMIT,
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentDeletionState,
)
from investment_research.modules.documents.persistence import DocumentDeletionRow

NOW = datetime(2026, 10, 12, tzinfo=UTC)


def test_deletion_requires_explicit_unique_bounded_ids() -> None:
    ids = tuple(uuid4() for _ in range(DOCUMENT_DELETION_LIMIT))
    assert DeleteDocumentsCommand(document_ids=ids).document_ids == ids
    for invalid in ((), (*ids, uuid4()), (ids[0], ids[0])):
        with pytest.raises(ValidationError):
            DeleteDocumentsCommand(document_ids=invalid)
    with pytest.raises(ValidationError):
        DeleteDocumentsCommand.model_validate({"document_ids": ["not-a-uuid"]})
    with pytest.raises(ValidationError):
        DeleteDocumentsCommand.model_validate(
            {"document_ids": [str(ids[0])], "workspace_id": str(uuid4())}
        )


@pytest.mark.parametrize(
    ("state", "completed_at"),
    [
        (DocumentDeletionState.PENDING, NOW),
        (DocumentDeletionState.COMPLETED, None),
        (DocumentDeletionState.COMPLETED, NOW - timedelta(seconds=1)),
    ],
)
def test_receipt_cannot_claim_unfinished_or_backdated_cleanup(
    state: DocumentDeletionState,
    completed_at: datetime | None,
) -> None:
    with pytest.raises(ValidationError):
        DocumentDeletionReceipt(
            workspace_id=uuid4(),
            document_id=uuid4(),
            state=state,
            requested_at=NOW,
            completed_at=completed_at,
        )


@pytest.mark.parametrize("state", list(DocumentDeletionState))
def test_receipt_round_trip_contains_no_source_content(state: DocumentDeletionState) -> None:
    receipt = DocumentDeletionReceipt(
        workspace_id=uuid4(),
        document_id=uuid4(),
        state=state,
        requested_at=NOW,
        completed_at=NOW if state is DocumentDeletionState.COMPLETED else None,
    )
    row = DocumentDeletionRow(**receipt.model_dump())
    assert row.to_contract() == receipt
    assert set(receipt.model_dump()) == {
        "workspace_id",
        "document_id",
        "state",
        "requested_at",
        "completed_at",
    }
    with pytest.raises(ValidationError):
        DocumentDeletionReceipt.model_validate({**receipt.model_dump(), "title": "private text"})


def test_receipt_outlives_document_but_remains_workspace_bound() -> None:
    table = cast(Table, DocumentDeletionRow.__table__)
    assert set(table.primary_key.columns.keys()) == {"workspace_id", "document_id"}
    references = {
        element.target_fullname
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }
    assert references == {"workspaces.id"}
