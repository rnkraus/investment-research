"""Exercise strict company HTTP contracts, bounded cursors, and safe failures."""

from base64 import urlsafe_b64encode
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, create_autospec
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.company_contracts import decode_company_cursor, encode_company_cursor
from investment_research.api.dependencies import get_company_service, get_workspace_context
from investment_research.main import create_app
from investment_research.modules.companies.application import CompanyService
from investment_research.modules.companies.contracts import (
    CompanyListCursor,
    CompanyListPage,
    CompanyListQuery,
    CompanySummary,
    CreateCompanyCommand,
)
from investment_research.modules.companies.domain import (
    CompanyNameIntegrityError,
    CompanyScopeError,
)
from investment_research.modules.companies.ports import (
    CompanyCommitUncertainError,
    CompanyNameConflictError,
    InvalidCompanyCursorError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.settings import ApiSettings

CONTEXT = WorkspaceContext(user_id=UUID(int=1), workspace_id=UUID(int=2))
SUMMARY = CompanySummary(
    company_id=UUID(int=3), name="Northstar Grid", created_at=datetime(2026, 11, 5, tzinfo=UTC)
)


@pytest.fixture
def company_client(tmp_path: Path) -> Iterator[tuple[TestClient, MagicMock]]:
    application = create_app(
        ApiSettings.model_validate(
            {
                "environment": "test",
                "database_url": "postgresql+psycopg://test-user:test-password@127.0.0.1:1/test",
                "redis_url": "redis://:synthetic-secret@127.0.0.1:1/0",
                "object_storage_root": tmp_path / "objects",
            }
        )
    )
    service = create_autospec(CompanyService, instance=True)
    service.create.return_value = SUMMARY
    service.list_companies.return_value = CompanyListPage(items=(SUMMARY,))
    application.dependency_overrides[get_company_service] = lambda: cast(CompanyService, service)
    application.dependency_overrides[get_workspace_context] = lambda: CONTEXT
    with TestClient(application) as client:
        yield client, service


def test_company_create_returns_safe_committed_summary(
    company_client: tuple[TestClient, MagicMock],
) -> None:
    client, service = company_client
    response = client.post("/api/v1/companies", json={"name": " Northstar   Grid "})

    assert response.status_code == 201
    assert response.json() == SUMMARY.model_dump(mode="json")
    assert response.headers["cache-control"] == "private, no-store"
    service.create.assert_called_once_with(CONTEXT, CreateCompanyCommand(name="Northstar Grid"))
    for field in ("workspace_id", "created_by", "normalized_name"):
        assert field not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": 123},
        {"name": "x" * 201},
        {"name": "private\x00value"},
        {"name": "private-value", "workspace_id": str(UUID(int=99))},
        {"name": "private-value", "created_by": str(UUID(int=99))},
        {"name": "private-value", "normalized_name": "private-value"},
    ],
)
def test_company_create_rejects_invalid_or_client_owned_fields(
    company_client: tuple[TestClient, MagicMock], payload: dict[str, object]
) -> None:
    client, service = company_client
    response = client.post("/api/v1/companies", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.headers["cache-control"] == "private, no-store"
    assert "private" not in response.text
    service.create.assert_not_called()


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        (CompanyNameConflictError("private detail"), "CONFLICT", 409),
        (CompanyCommitUncertainError("private detail"), "INTERNAL_ERROR", 500),
        (SQLAlchemyError("private detail"), "INTERNAL_ERROR", 500),
        (CompanyScopeError("private detail"), "INTERNAL_ERROR", 500),
        (CompanyNameIntegrityError("private detail"), "INTERNAL_ERROR", 500),
    ],
)
def test_company_create_classifies_errors_without_echoing_internal_details(
    company_client: tuple[TestClient, MagicMock], error: Exception, code: str, status: int
) -> None:
    client, service = company_client
    service.create.side_effect = error
    response = client.post("/api/v1/companies", json={"name": "private company name"})

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "private, no-store"
    assert "private" not in response.text
    assert service.create.call_count == 1


def test_company_listing_encodes_and_accepts_a_query_bound_cursor(
    company_client: tuple[TestClient, MagicMock],
) -> None:
    client, service = company_client
    query = CompanyListQuery(q="Grid", page_size=1)
    cursor = CompanyListCursor(
        company_id=SUMMARY.company_id, query_fingerprint=query.fingerprint(CONTEXT.workspace_id)
    )
    service.list_companies.return_value = CompanyListPage(items=(SUMMARY,), next_cursor=cursor)
    response = client.get("/api/v1/companies", params={"q": " Grid ", "page_size": 1})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    token = response.json()["next_cursor"]
    assert isinstance(token, str) and decode_company_cursor(token) == cursor
    assert response.json()["items"] == [SUMMARY.model_dump(mode="json")]
    service.list_companies.assert_called_once_with(CONTEXT, query)
    service.list_companies.reset_mock()
    service.list_companies.return_value = CompanyListPage(items=())

    second = client.get("/api/v1/companies", params={"q": "GRID", "page_size": 1, "cursor": token})

    assert second.json() == {"items": [], "next_cursor": None}
    service.list_companies.assert_called_once_with(
        CONTEXT, CompanyListQuery(q="GRID", page_size=1, after=cursor)
    )


@pytest.mark.parametrize(
    "query",
    [
        "page_size=0",
        "page_size=101",
        "page_size=invalid",
        "page_size=1&page_size=2",
        "q=one&q=two",
        "cursor=one&cursor=two",
        "q=%00private-value",
        "q=" + "x" * 201,
        "cursor=",
        "cursor=" + "a" * 4097,
        "cursor=not-json",
        "workspace_id=private-value",
        "unexpected=private-value",
    ],
)
def test_company_listing_rejects_invalid_queries_before_service_calls(
    company_client: tuple[TestClient, MagicMock],
    query: str,
) -> None:
    client, service = company_client
    response = client.get(f"/api/v1/companies?{query}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.headers["cache-control"] == "private, no-store"
    assert "private-value" not in response.text
    service.list_companies.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (InvalidCompanyCursorError("private detail"), 422),
        (SQLAlchemyError("private detail"), 500),
        (CompanyNameIntegrityError("private detail"), 500),
    ],
)
def test_company_listing_hides_storage_and_cursor_details(
    company_client: tuple[TestClient, MagicMock],
    error: Exception,
    status: int,
) -> None:
    client, service = company_client
    service.list_companies.side_effect = error
    response = client.get("/api/v1/companies")

    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert "private detail" not in response.text
    assert "items" not in response.json()


@pytest.mark.parametrize("streamed", [False, True])
def test_company_body_limit_rejects_declared_and_streamed_oversize_before_creation(
    company_client: tuple[TestClient, MagicMock],
    streamed: bool,
) -> None:
    client, service = company_client
    body = b'{"name":"' + b"x" * (64 * 1024) + b'"}'
    response = client.post(
        "/api/v1/companies",
        content=iter((body[:32000], body[32000:])) if streamed else body,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert response.headers["cache-control"] == "private, no-store"
    service.create.assert_not_called()


def test_company_creation_rejects_query_scope(company_client: tuple[TestClient, MagicMock]) -> None:
    client, service = company_client
    response = client.post(
        "/api/v1/companies?workspace_id=private-value", json={"name": "Northstar"}
    )
    assert response.status_code == 422
    service.create.assert_not_called()


def test_company_body_limit_allows_a_valid_body_at_the_boundary(
    company_client: tuple[TestClient, MagicMock],
) -> None:
    client, service = company_client
    body = b'{"name":"Northstar Grid"}'
    body += b" " * (64 * 1024 - len(body))
    response = client.post(
        "/api/v1/companies",
        content=iter((body[:32000], body[32000:])),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 201
    service.create.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"{}",
        b"\xff",
        b'{"version":"other","company_id":"00000000-0000-0000-0000-000000000001","query_fingerprint":"'
        + b"a" * 64
        + b'"}',
    ],
)
def test_company_cursor_rejects_untrusted_decoded_shapes(body: bytes) -> None:
    token = urlsafe_b64encode(body).decode("ascii").rstrip("=")
    with pytest.raises(InvalidCompanyCursorError):
        decode_company_cursor(token)


def test_company_cursor_rejects_duplicate_fields_and_noncanonical_json() -> None:
    cursor = CompanyListCursor(company_id=UUID(int=1), query_fingerprint="a" * 64)
    assert decode_company_cursor(encode_company_cursor(cursor)) == cursor
    body = cursor.model_dump_json()
    for altered in (" " + body, body[:-1] + ',"version":"companies-name-v1"}'):
        token = urlsafe_b64encode(altered.encode()).decode("ascii").rstrip("=")
        with pytest.raises(InvalidCompanyCursorError):
            decode_company_cursor(token)
