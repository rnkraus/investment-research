"""Bound company discovery inputs and preserve scope/query continuation semantics."""

from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.companies.application import CompanyService
from investment_research.modules.companies.contracts import (
    CompanyListCursor,
    CompanyListPage,
    CompanyListQuery,
)
from investment_research.modules.companies.ports import CompanyRepository
from investment_research.modules.identity.domain import WorkspaceContext
from tests.unit.test_company_application import RecordingUnitOfWork


@pytest.mark.parametrize("size", [0, -1, 101, True, 1.5, "20"])
def test_company_listing_rejects_unbounded_or_coerced_page_sizes(size: object) -> None:
    with pytest.raises(ValidationError):
        CompanyListQuery.model_validate({"page_size": size})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("\u00a0", None),
        (" Cafe\u0301   RESEARCH ", "café research"),
        ("Straße AG", "strasse ag"),
        ("%_/", "%_/"),
        ("\u0390" * 200, "\u0390".casefold() * 200),
    ],
)
def test_company_search_uses_name_comparison_rules_and_literal_input(
    value: str | None, expected: str | None
) -> None:
    query = CompanyListQuery(q=value)

    assert query.normalized_query == expected
    assert query.page_size == 20
    assert CompanyListQuery(page_size=100).page_size == 100


@pytest.mark.parametrize("value", ["\t", "\n", "a\x00", "a\u200b", "x" * 201, 1, True])
def test_company_search_rejects_controls_oversized_names_and_non_text(value: object) -> None:
    with pytest.raises(ValidationError) as caught:
        CompanyListQuery.model_validate({"q": value})

    assert "input_value" not in str(caught.value)


def test_company_query_rejects_client_owned_scope() -> None:
    with pytest.raises(ValidationError):
        CompanyListQuery.model_validate({"workspace_id": str(UUID(int=2))})


def test_company_fingerprint_binds_scope_filter_size_and_normalized_equivalence() -> None:
    workspace = UUID(int=2)
    query = CompanyListQuery(q="Straße AG", page_size=2)
    fingerprint = query.fingerprint(workspace)

    assert CompanyListQuery(q=" STRASSE   AG ", page_size=2).fingerprint(workspace) == fingerprint
    assert query.fingerprint(UUID(int=3)) != fingerprint
    assert CompanyListQuery(q="Other", page_size=2).fingerprint(workspace) != fingerprint
    assert CompanyListQuery(q="Straße AG", page_size=3).fingerprint(workspace) != fingerprint
    assert CompanyListQuery(q=" ").fingerprint(workspace) == CompanyListQuery().fingerprint(
        workspace
    )


@pytest.mark.parametrize("fingerprint", ["a" * 63, "a" * 65, "z" * 64, "a" * 64 + "\n"])
def test_company_cursor_rejects_invalid_fingerprints(fingerprint: str) -> None:
    with pytest.raises(ValidationError):
        CompanyListCursor(company_id=UUID(int=1), query_fingerprint=fingerprint)


def test_company_cursor_is_versioned_and_contains_no_names_or_search_text() -> None:
    cursor = CompanyListCursor(company_id=UUID(int=1), query_fingerprint="a" * 64)

    assert set(cursor.model_dump()) == {"version", "company_id", "query_fingerprint"}
    with pytest.raises(ValidationError):
        CompanyListCursor.model_validate({**cursor.model_dump(), "version": "other-v1"})
    with pytest.raises(ValidationError):
        CompanyListCursor.model_validate({**cursor.model_dump(), "name": "private name"})
    assert CompanyListQuery(after=cursor).fingerprint(UUID(int=2)) == (
        CompanyListQuery().fingerprint(UUID(int=2))
    )


def test_company_listing_uses_only_the_scoped_read_unit() -> None:
    repository = create_autospec(CompanyRepository, instance=True)
    page = CompanyListPage(items=())
    repository.list_companies.return_value = page
    read = RecordingUnitOfWork(cast(CompanyRepository, repository))
    write = RecordingUnitOfWork(cast(CompanyRepository, repository))
    service = CompanyService(
        write_unit_of_work_factory=write.open, read_unit_of_work_factory=read.open
    )
    context = WorkspaceContext(workspace_id=UUID(int=2), user_id=UUID(int=1))
    query = CompanyListQuery(q="Research", page_size=2)

    assert service.list_companies(context, query) == page

    repository.list_companies.assert_called_once_with(context, query)
    assert read.events == ["entered", "completed", "closed"]
    assert write.events == []
    repository.add.assert_not_called()
