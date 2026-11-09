"""Verify attribution corrections against canonical versions in PostgreSQL."""

from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from investment_research.api.dependencies import get_workspace_context
from investment_research.main import create_app
from investment_research.modules.documents.persistence import DocumentVersionRow
from investment_research.settings import ApiSettings
from tests.integration.test_document_listing_postgresql import add_workspace


def test_edit_and_clear_source_attribution_without_rewriting_the_original(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    with Session(integration_database_engine) as session, session.begin():
        owner = add_workspace(session)
        other = add_workspace(session)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: owner

    with TestClient(application) as client:
        original = "Northstar announces a new transformer pilot."
        capture = client.post(
            "/api/v1/documents/text",
            json={"title": "Pilot update", "text": original, "author_name": "Captured author"},
        )
        assert capture.status_code == 202
        document_id = capture.json()["document_id"]
        version_id = capture.json()["document_version_id"]
        path = f"/api/v1/documents/{document_id}/source-attribution"
        initial = client.get("/api/v1/documents").json()["items"][0]
        assert initial["author_name"] == "Captured author"
        payload = {
            "document_version_id": version_id,
            "author_name": "Corrected author",
            "x_handle": "@NorthstarIR",
        }

        application.dependency_overrides[get_workspace_context] = lambda: other
        assert client.put(path, json=payload).status_code == 404
        application.dependency_overrides[get_workspace_context] = lambda: owner

        assert (
            client.put(path, json={**payload, "document_version_id": str(uuid4())}).status_code
            == 409
        )
        assert (
            client.put(path, json={**payload, "x_handle": "https://x.com/NorthstarIR"}).status_code
            == 422
        )
        saved = client.put(path, json=payload)
        assert saved.status_code == 200
        assert saved.headers["cache-control"] == "private, no-store"
        assert saved.json() == {"document_id": document_id, **payload}
        assert client.put(path, json=payload).json() == saved.json()
        edited = client.get("/api/v1/documents").json()["items"][0]
        assert edited["author_name"] == "Corrected author"
        assert edited["x_handle"] == "@NorthstarIR"
        assert edited["processing"] == initial["processing"]
        assert edited["document_version_id"] == version_id
        assert client.get(initial["original_url"]).text == original

        assert (
            client.put(path, json={**payload, "author_name": None, "x_handle": None}).status_code
            == 200
        )
        cleared = client.get("/api/v1/documents").json()["items"][0]
        assert cleared["author_name"] is None
        assert cleared["x_handle"] is None

    with Session(integration_database_engine) as session:
        version = session.get(DocumentVersionRow, UUID(version_id))
        assert version is not None
        assert version.author_name == "Captured author"
        assert version.x_handle is None
