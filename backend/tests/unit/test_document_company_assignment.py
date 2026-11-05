"""Reject ambiguous or oversized replacement commands before entering a transaction."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.company_assignment import ReplaceDocumentCompaniesCommand


@pytest.mark.parametrize(
    "invalid",
    [
        {"company_ids": [UUID(int=1), UUID(int=1)]},
        {"company_ids": [UUID(int=index) for index in range(21)]},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"expected_revision": 2**63},
        {"reviewed_by": UUID(int=1)},
    ],
)
def test_invalid_company_replacement_fields(invalid: dict[str, object]) -> None:
    payload = {
        "expected_document_version_id": UUID(int=2),
        "expected_revision": 0,
        "company_ids": [],
    }
    with pytest.raises(ValidationError):
        ReplaceDocumentCompaniesCommand.model_validate({**payload, **invalid})
