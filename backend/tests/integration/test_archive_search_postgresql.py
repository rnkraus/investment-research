"""Exercise read-only archive discovery through the real API and PostgreSQL."""

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from investment_research.api.dependencies import get_workspace_context
from investment_research.main import create_app
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.research.persistence import ResearchRequestRow
from investment_research.settings import ApiSettings
from tests.integration.factories import (
    FIXTURE_TIME,
    make_searchable_research_source,
    persist_research_source,
)
from tests.integration.test_document_company_service_postgresql import add_company


def test_archive_search_returns_scoped_phrase_matches_without_saving_questions(
    integration_database_engine: Engine,
    integration_api_settings: ApiSettings,
    tmp_path: Path,
) -> None:
    engine = integration_database_engine
    passage = "Capacity expansion remained on track."
    source, foreign = (
        make_searchable_research_source(
            label=label,
            primary_text=passage,
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
        )
        for label in ("Archive search owner", "Archive search foreign")
    )
    for item in (source, foreign):
        persist_research_source(engine, item)
    company_id = add_company(engine, source.context)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    with TestClient(application) as client:
        assigned = client.put(
            f"/api/v1/documents/{source.document.id}/companies",
            json={
                "expected_document_version_id": str(source.version.id),
                "expected_revision": 0,
                "company_ids": [str(company_id)],
            },
        )
        assert assigned.status_code == 200
        route = "/api/v1/archive-search"
        query = {"query": '"Capacity expansion"'}
        unfiltered = client.post(route, json=query)
        filtered = client.post(
            route,
            json={
                **query,
                "company_id": str(company_id),
                "published_from": FIXTURE_TIME.date().isoformat(),
                "published_to": FIXTURE_TIME.date().isoformat(),
            },
        )
        assert unfiltered.status_code == filtered.status_code == 200
        assert filtered.headers["cache-control"] == "private, no-store"
        assert filtered.json() == unfiltered.json()
        assert filtered.json() == {
            "items": [
                {
                    "chunk_id": str(source.chunks[0].id),
                    "document_id": str(source.document.id),
                    "document_version_id": str(source.version.id),
                    "title": source.version.title,
                    "published_at": FIXTURE_TIME.isoformat().replace("+00:00", "Z"),
                    "source_locator_kind": "PDF_PAGE",
                    "page_number": 2,
                    "passage": passage,
                }
            ],
            "has_more": False,
        }
        for payload in ({**query, "company_id": str(uuid4())}, {"query": '"expansion capacity"'}):
            assert client.post(route, json=payload).json() == {"items": [], "has_more": False}
        assert client.post(route, json={"query": "   "}).status_code == 422
        assert client.post(route, json={**query, "workspace_id": str(uuid4())}).status_code == 422

    with Session(engine) as session:
        count = session.scalar(
            select(func.count())
            .select_from(ResearchRequestRow)
            .where(ResearchRequestRow.workspace_id == source.context.workspace_id)
        )
    assert count == 1  # Only the existing fixture request.
