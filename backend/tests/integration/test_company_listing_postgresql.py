"""Prove literal scoped company discovery and stable, query-bound continuation."""

from uuid import uuid4

import pytest
from sqlalchemy import Engine

from investment_research.modules.companies.contracts import (
    CompanyListCursor,
    CompanyListQuery,
    CreateCompanyCommand,
)
from investment_research.modules.companies.ports import InvalidCompanyCursorError
from tests.integration.test_company_service_postgresql import company_service
from tests.integration.test_document_capture_postgresql import create_workspace


def test_company_listing_filters_before_paging_and_excludes_foreign_rows(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    foreign = create_workspace(engine)
    empty = create_workspace(engine)
    service = company_service(engine)
    names = ("alpha Research", "Beta Research", "Café Research", "Delta Research", "Zulu Research")
    for name in (*reversed(names), "Alpha Other", "B Other", "C Other"):
        service.create(context, CreateCompanyCommand(name=name))
    service.create(foreign, CreateCompanyCommand(name="Aardvark Research"))

    page = service.list_companies(context, CompanyListQuery(q="RESEARCH", page_size=2))
    assert tuple(item.name for item in page.items) == names[:2]
    assert page.next_cursor is not None
    second = service.list_companies(
        context, CompanyListQuery(q="research", page_size=2, after=page.next_cursor)
    )
    assert tuple(item.name for item in second.items) == names[2:4]
    assert second.next_cursor is not None
    last = service.list_companies(
        context, CompanyListQuery(q=" Research ", page_size=2, after=second.next_cursor)
    )
    assert tuple(item.name for item in last.items) == names[4:]
    assert last.next_cursor is None
    assert service.list_companies(empty, CompanyListQuery()).items == ()
    assert service.list_companies(context, CompanyListQuery(q="unknown")).items == ()


def test_company_keyset_does_not_repeat_rows_after_an_earlier_name_is_inserted(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    service = company_service(engine)
    for name in ("Alpha", "Beta", "Delta", "Echo"):
        service.create(context, CreateCompanyCommand(name=name))
    first = service.list_companies(context, CompanyListQuery(page_size=2))
    assert tuple(item.name for item in first.items) == ("Alpha", "Beta")
    assert first.next_cursor is not None
    service.create(context, CreateCompanyCommand(name="Aardvark"))
    service.create(context, CreateCompanyCommand(name="Charlie"))

    second = service.list_companies(context, CompanyListQuery(page_size=2, after=first.next_cursor))
    assert tuple(item.name for item in second.items) == ("Charlie", "Delta")
    assert second.next_cursor is not None
    last = service.list_companies(context, CompanyListQuery(page_size=2, after=second.next_cursor))
    assert tuple(item.name for item in last.items) == ("Echo",)
    assert last.next_cursor is None


@pytest.mark.parametrize("literal", ["%", "_", "/", "\\"])
def test_company_search_escapes_sql_wildcards_and_escape_characters(
    integration_database_engine: Engine, literal: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    service = company_service(engine)
    for separator in ("%", "_", "/", "\\", "X"):
        service.create(context, CreateCompanyCommand(name=f"Alpha{separator}Growth"))

    page = service.list_companies(context, CompanyListQuery(q=literal, page_size=1))

    assert tuple(item.name for item in page.items) == (f"Alpha{literal}Growth",)
    assert page.next_cursor is None


def test_company_search_matches_unicode_casefold_without_removing_accents(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    service = company_service(engine)
    company = service.create(context, CreateCompanyCommand(name="Straße Café"))

    assert service.list_companies(context, CompanyListQuery(q="STRASSE")).items == (company,)
    assert service.list_companies(context, CompanyListQuery(q="Cafe\u0301")).items == (company,)
    assert service.list_companies(context, CompanyListQuery(q="Cafe")).items == ()


@pytest.mark.parametrize("change", ["workspace", "filter", "page_size"])
def test_company_cursor_rejects_changed_scope_query_or_page_size(
    integration_database_engine: Engine, change: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    service = company_service(engine)
    for name in ("Alpha Grid", "Beta Grid"):
        service.create(context, CreateCompanyCommand(name=name))
    page = service.list_companies(context, CompanyListQuery(q="Grid", page_size=1))
    assert page.next_cursor is not None

    with pytest.raises(InvalidCompanyCursorError, match=r"^company cursor is invalid$"):
        service.list_companies(
            create_workspace(engine) if change == "workspace" else context,
            CompanyListQuery(
                q="Other" if change == "filter" else "Grid",
                page_size=2 if change == "page_size" else 1,
                after=page.next_cursor,
            ),
        )


@pytest.mark.parametrize("anchor_kind", ["unknown", "foreign", "outside_filter"])
def test_company_cursor_is_not_authorization_even_with_a_matching_fingerprint(
    integration_database_engine: Engine, anchor_kind: str
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    foreign = create_workspace(engine)
    service = company_service(engine)
    other = service.create(foreign, CreateCompanyCommand(name="Other Grid"))
    excluded = service.create(context, CreateCompanyCommand(name="Unrelated Company"))
    identifiers = {
        "unknown": uuid4(),
        "foreign": other.company_id,
        "outside_filter": excluded.company_id,
    }
    query = CompanyListQuery(q="Grid")
    cursor = CompanyListCursor(
        company_id=identifiers[anchor_kind],
        query_fingerprint=query.fingerprint(context.workspace_id),
    )

    with pytest.raises(InvalidCompanyCursorError, match=r"^company cursor is invalid$"):
        service.list_companies(context, CompanyListQuery(q="Grid", after=cursor))
