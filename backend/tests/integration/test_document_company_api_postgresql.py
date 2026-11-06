"""Exercise company assignment HTTP wiring against migrated PostgreSQL."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from investment_research.api.dependencies import get_workspace_context
from investment_research.api.middleware import PRIVATE_JSON_BODY_LIMIT_BYTES
from investment_research.main import create_app
from investment_research.settings import ApiSettings
from tests.integration.test_document_company_service_postgresql import add_company
from tests.integration.test_document_deletion_postgresql import persist_unclaimed_source


def test_document_company_api_round_trip_conflict_and_workspace_scope(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    foreign = persist_unclaimed_source(engine)
    company = add_company(engine, source.context)
    foreign_company = add_company(engine, foreign.context)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    path = f"/api/v1/documents/{source.document.id}/companies"

    with TestClient(application) as client:
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.headers["cache-control"] == "private, no-store"
        assert initial.json() == {
            "document_id": str(source.document.id),
            "document_version_id": str(source.version.id),
            "revision": 0,
            "companies": [],
        }
        payload = {
            "expected_document_version_id": str(source.version.id),
            "expected_revision": 0,
            "company_ids": [str(company)],
        }
        saved = client.put(path, json=payload)
        assert saved.status_code == 200
        assert saved.headers["cache-control"] == "private, no-store"
        assert saved.json()["revision"] == 1
        assert [item["company_id"] for item in saved.json()["companies"]] == [str(company)]
        assert client.get(path).json() == saved.json()

        stale = client.put(path, json=payload)
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CONFLICT"
        assert stale.headers["cache-control"] == "private, no-store"

        for unavailable in (foreign_company, uuid4()):
            missing = client.put(
                path,
                json={
                    **payload,
                    "expected_revision": 1,
                    "company_ids": [str(unavailable)],
                },
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["message"] == "assignment target is unavailable"
            assert str(unavailable) not in missing.text
        assert client.get(path).json() == saved.json()

        cleared = client.put(path, json={**payload, "expected_revision": 1, "company_ids": []})
        assert cleared.status_code == 200
        assert cleared.json()["revision"] == 2 and cleared.json()["companies"] == []
        assert client.get(path).json() == cleared.json()

        application.dependency_overrides[get_workspace_context] = lambda: foreign.context
        hidden = client.get(path)
        assert hidden.status_code == 404
        assert hidden.headers["cache-control"] == "private, no-store"
        assert str(source.document.id) not in hidden.text


def test_document_company_api_rejects_untrusted_input_and_oversize_bodies(
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    path = f"/api/v1/documents/{uuid4()}/companies"
    workspace_id = str(uuid4())
    payload = {
        "expected_document_version_id": str(uuid4()),
        "expected_revision": 0,
        "company_ids": [],
        "workspace_id": workspace_id,
    }
    with TestClient(application) as client:
        invalid = client.put(path, json=payload)
        assert invalid.status_code == 422
        assert workspace_id not in invalid.text
        assert invalid.headers["cache-control"] == "private, no-store"
        assert client.get(path, params={"workspace_id": workspace_id}).status_code == 422

        body = b" " * (PRIVATE_JSON_BODY_LIMIT_BYTES + 1)
        for content in (body, iter((body[:32000], body[32000:]))):
            oversized = client.put(
                path, content=content, headers={"content-type": "application/json"}
            )
            assert oversized.status_code == 413
            assert oversized.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
            assert oversized.headers["cache-control"] == "private, no-store"
