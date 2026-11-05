"""Use production company HTTP wiring against an isolated migrated database."""

from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from investment_research.api.company_contracts import encode_company_cursor
from investment_research.api.dependencies import get_workspace_context
from investment_research.main import create_app
from investment_research.modules.companies.contracts import CompanyListCursor, CompanyListQuery
from investment_research.modules.companies.persistence import CompanyRow
from investment_research.settings import ApiSettings
from tests.integration.test_document_capture_postgresql import create_workspace
from tests.integration.test_document_deletion_postgresql import persist_unclaimed_source


def test_company_api_commits_and_paginates_without_crossing_workspace_boundaries(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    engine = integration_database_engine
    owner = create_workspace(engine)
    foreign = create_workspace(engine)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: owner
    with TestClient(application) as client:
        created = [
            client.post("/api/v1/companies", json={"name": name})
            for name in ("Zulu Grid", " Alpha   Grid ", "Beta Grid")
        ]
        assert all(response.status_code == 201 for response in created)
        assert all(response.headers["cache-control"] == "private, no-store" for response in created)
        duplicate = client.post("/api/v1/companies", json={"name": "ALPHA GRID"})
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "CONFLICT"
        first = client.get("/api/v1/companies", params={"q": "Grid", "page_size": 2})
        assert first.status_code == 200
        assert [item["name"] for item in first.json()["items"]] == ["Alpha Grid", "Beta Grid"]
        cursor = first.json()["next_cursor"]
        assert isinstance(cursor, str)
        second = client.get(
            "/api/v1/companies", params={"q": "GRID", "page_size": 2, "cursor": cursor}
        )
        assert [item["name"] for item in second.json()["items"]] == ["Zulu Grid"]
        assert second.json()["next_cursor"] is None

        application.dependency_overrides[get_workspace_context] = lambda: foreign
        assert client.get("/api/v1/companies").json() == {"items": [], "next_cursor": None}
        foreign_create = client.post("/api/v1/companies", json={"name": "Alpha Grid"})
        assert foreign_create.status_code == 201
        assert foreign_create.json()["company_id"] != created[1].json()["company_id"]
        invalid = client.get(
            "/api/v1/companies", params={"q": "Grid", "page_size": 2, "cursor": cursor}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["message"] == "company cursor is invalid"
        assert invalid.headers["cache-control"] == "private, no-store"

    with Session(engine) as session:
        for response in created:
            row = session.get(CompanyRow, UUID(response.json()["company_id"]))
            assert row is not None and row.workspace_id == owner.workspace_id
            assert row.created_by == owner.user_id


def test_company_api_rechecks_canonical_scope_even_for_a_forged_matching_fingerprint(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    engine = integration_database_engine
    owner = create_workspace(engine)
    foreign = create_workspace(engine)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: foreign
    with TestClient(application) as client:
        created = client.post("/api/v1/companies", json={"name": "Private Synthetic Company"})
        assert created.status_code == 201
        query = CompanyListQuery()
        token = encode_company_cursor(
            CompanyListCursor(
                company_id=UUID(created.json()["company_id"]),
                query_fingerprint=query.fingerprint(owner.workspace_id),
            )
        )
        application.dependency_overrides[get_workspace_context] = lambda: owner
        response = client.get("/api/v1/companies", params={"cursor": token})

        assert response.status_code == 422
        assert response.json()["error"]["message"] == "company cursor is invalid"
        assert "Private Synthetic Company" not in response.text
        assert str(foreign.workspace_id) not in response.text
        assert client.get("/api/v1/companies").json() == {"items": [], "next_cursor": None}


def test_company_ticker_create_update_search_and_scope(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    owner = create_workspace(integration_database_engine)
    foreign = create_workspace(integration_database_engine)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: owner
    with TestClient(application) as client:
        first = client.post(
            "/api/v1/companies", json={"name": "Alpha Research", "ticker": " brk.b "}
        )
        assert first.status_code == 201 and first.json()["ticker"] == "BRK.B"
        existing = client.post("/api/v1/companies", json={"name": "Beta Research"})
        assert existing.status_code == 201 and existing.json()["ticker"] is None
        company_id = existing.json()["company_id"]
        path = f"/api/v1/companies/{company_id}/ticker"
        updated = client.patch(path, json={"ticker": "brk.b"})
        assert updated.status_code == 200 and updated.json() == {
            **existing.json(),
            "ticker": "BRK.B",
        }
        assert updated.headers["cache-control"] == "private, no-store"

        page = client.get("/api/v1/companies", params={"q": "brk.b", "page_size": 1})
        assert page.status_code == 200 and page.json()["items"] == [first.json()]
        cursor = page.json()["next_cursor"]
        assert isinstance(cursor, str)
        second = client.get(
            "/api/v1/companies", params={"q": "BRK.B", "page_size": 1, "cursor": cursor}
        )
        assert second.status_code == 200 and second.json()["items"] == [updated.json()]

        application.dependency_overrides[get_workspace_context] = lambda: foreign
        assert client.patch(path, json={"ticker": "NVDA"}).status_code == 404
        assert client.get("/api/v1/companies", params={"q": "BRK.B"}).json()["items"] == []
        application.dependency_overrides[get_workspace_context] = lambda: owner
        cleared = client.patch(path, json={"ticker": None})
        assert cleared.status_code == 200 and cleared.json() == existing.json()


def test_company_rename_preserves_links_and_rejects_duplicate_names(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    source = persist_unclaimed_source(integration_database_engine)
    foreign = create_workspace(integration_database_engine)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    assignment_path = f"/api/v1/documents/{source.document.id}/companies"
    with TestClient(application) as client:
        original = client.post(
            "/api/v1/companies", json={"name": "Alpha Research", "ticker": "ALP"}
        )
        assert original.status_code == 201
        assert client.post("/api/v1/companies", json={"name": "Beta Research"}).status_code == 201
        company_id = original.json()["company_id"]
        path = f"/api/v1/companies/{company_id}"
        saved = client.put(
            assignment_path,
            json={
                "expected_document_version_id": str(source.version.id),
                "expected_revision": 0,
                "company_ids": [company_id],
            },
        )
        assert saved.status_code == 200

        duplicate = client.patch(path, json={"name": " beta  research ", "ticker": "NEW"})
        assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "CONFLICT"
        assert client.get(assignment_path).json() == saved.json()
        for invalid in ({}, {"name": None}, {"name": "   "}):
            assert client.patch(path, json=invalid).status_code == 422

        renamed = client.patch(path, json={"name": " Alpha   Energy "})
        assert renamed.status_code == 200
        assert renamed.json() == {**original.json(), "name": "Alpha Energy"}
        assert renamed.headers["cache-control"] == "private, no-store"
        assert client.get(assignment_path).json() == {**saved.json(), "companies": [renamed.json()]}
        assert client.get("/api/v1/companies", params={"q": "Alpha Energy"}).json()["items"] == [
            renamed.json()
        ]
        assert client.get("/api/v1/companies", params={"q": "Alpha Research"}).json()["items"] == []

        application.dependency_overrides[get_workspace_context] = lambda: foreign
        assert client.patch(path, json={"name": "Hidden"}).status_code == 404
