"""PostgreSQL API tests for validated research streams and workspace isolation."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from investment_research.api.dependencies import get_workspace_context
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
)
from investment_research.main import create_app
from investment_research.modules.evidence.persistence import (
    EvidenceSpanRow,
    StatementEvidenceRow,
)
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.settings import ApiSettings
from tests.integration.factories import (
    ResearchSource,
    make_research_source,
    make_searchable_research_source,
    persist_research_source,
    persist_workspace,
)

UNANSWERABLE_QUESTION = "What dividend did Northstar Grid Analytics declare for FY2025?"


def make_manifest_source(*, label: str) -> ResearchSource:
    """Build the synthetic source text required by the deterministic answer path."""

    answerable_text = (
        f"{M0_0_FIXTURE_ANSWER_CASE.question} {M0_0_FIXTURE_ANSWER_CASE.evidence_text}"
    )
    return make_searchable_research_source(
        label=label,
        primary_text=answerable_text,
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
    )


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode terminal events independently of live research progress."""

    decoded: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        if not lines or lines[0] in {"event: research.progress", "event: research.context"}:
            continue
        decoded.append(
            {
                "event_name": lines[0].removeprefix("event: "),
                "payload": json.loads(lines[1].removeprefix("data: ")),
            }
        )
    return decoded


def test_research_api_emits_the_validated_manifest_answer(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_manifest_source(label="Research API Answer")
    persist_research_source(integration_database_engine, source)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == [
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]
    assert events[0]["payload"]["text"] == M0_0_FIXTURE_ANSWER_CASE.statement
    assert events[1]["payload"]["relationship"] == "SUPPORTS"
    assert events[2]["payload"]["coverage"] == "COMPLETE"
    assert all(
        event["payload"]["research_request_id"] == events[0]["payload"]["research_request_id"]
        for event in events
    )


def test_research_api_reuses_canonical_evidence_for_repeated_answers(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_manifest_source(label="Research API Repeated Answer")
    persist_research_source(integration_database_engine, source)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    request_body = {
        "question": M0_0_FIXTURE_ANSWER_CASE.question,
        "context": {"document_ids": [str(source.document.id)]},
    }

    with TestClient(application) as client:
        first_response = client.post("/api/v1/research-requests", json=request_body)
        second_response = client.post("/api/v1/research-requests", json=request_body)

    first_events = parse_server_sent_events(first_response.text)
    second_events = parse_server_sent_events(second_response.text)
    first_citation = first_events[1]["payload"]
    second_citation = second_events[1]["payload"]
    assert first_response.status_code == second_response.status_code == 200
    assert first_citation["evidence_span_id"] == second_citation["evidence_span_id"]
    assert first_citation["research_request_id"] != second_citation["research_request_id"]

    with integration_database_engine.connect() as connection:
        span_count = connection.scalar(
            select(func.count())
            .select_from(EvidenceSpanRow)
            .where(EvidenceSpanRow.workspace_id == source.context.workspace_id)
        )
        relationship_count = connection.scalar(
            select(func.count())
            .select_from(StatementEvidenceRow)
            .where(StatementEvidenceRow.workspace_id == source.context.workspace_id)
        )

    assert span_count == 1
    assert relationship_count == 2


def test_research_api_emits_no_content_for_the_unanswerable_manifest_question(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_manifest_source(label="Research API Insufficient")
    persist_research_source(integration_database_engine, source)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": UNANSWERABLE_QUESTION,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["answer.insufficient"]
    assert events[0]["payload"]["coverage"] == "INSUFFICIENT"
    assert events[0]["payload"]["message"] == M0_0_INSUFFICIENT_MESSAGE
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text


def test_research_api_hides_foreign_documents_like_unknown_documents(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    owner = make_manifest_source(label="Research API Owner")
    foreign = make_research_source(label="Research API Foreign")
    persist_research_source(integration_database_engine, owner)
    persist_workspace(integration_database_engine, foreign)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: foreign.context

    with TestClient(application) as client:
        foreign_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(owner.document.id)]},
            },
        )
        unknown_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(uuid4())]},
            },
        )

    foreign_event = parse_server_sent_events(foreign_response.text)[0]
    unknown_event = parse_server_sent_events(unknown_response.text)[0]
    assert foreign_response.status_code == unknown_response.status_code == 200
    assert foreign_event["event_name"] == unknown_event["event_name"] == "answer.insufficient"
    assert foreign_event["payload"]["coverage"] == unknown_event["payload"]["coverage"]
    assert foreign_event["payload"]["message"] == unknown_event["payload"]["message"]
    assert "answer.statement" not in foreign_response.text
    assert "answer.citation" not in foreign_response.text


def test_evidence_api_resolves_a_streamed_citation_to_its_exact_active_source(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    source = make_manifest_source(label="Evidence API Source")
    persist_research_source(integration_database_engine, source)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        answer_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )
        citation_event = parse_server_sent_events(answer_response.text)[1]
        evidence_response = client.get(
            f"/api/v1/evidence-spans/{citation_event['payload']['evidence_span_id']}"
        )

    payload = evidence_response.json()
    expected_start = len(M0_0_FIXTURE_ANSWER_CASE.question) + 1
    assert answer_response.status_code == 200
    assert citation_event["event_name"] == "answer.citation"
    assert evidence_response.status_code == 200
    assert payload["evidence_span_id"] == citation_event["payload"]["evidence_span_id"]
    assert payload["document_id"] == str(source.document.id)
    assert payload["document_version_id"] == str(source.version.id)
    assert payload["locator"] == {"kind": "PDF_PAGE", "page": 2}
    assert payload["start_character"] == expected_start
    assert payload["end_character"] == (
        expected_start + len(M0_0_FIXTURE_ANSWER_CASE.evidence_text)
    )
    assert payload["exact_text"] == M0_0_FIXTURE_ANSWER_CASE.evidence_text
    assert payload["viewer_url"] == f"/sources/{payload['evidence_span_id']}"
    assert evidence_response.headers["cache-control"] == "private, no-store"
    assert "storage_key" not in evidence_response.text


def test_evidence_api_hides_a_foreign_citation_like_an_unknown_span(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    tmp_path: Path,
) -> None:
    owner = make_manifest_source(label="Evidence API Owner")
    foreign = make_research_source(label="Evidence API Foreign")
    persist_research_source(integration_database_engine, owner)
    persist_workspace(integration_database_engine, foreign)
    application = create_app(
        integration_api_settings.model_copy(update={"object_storage_root": tmp_path / "objects"})
    )
    application.dependency_overrides[get_workspace_context] = lambda: owner.context

    with TestClient(application) as client:
        answer_response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(owner.document.id)]},
            },
        )
        citation_event = parse_server_sent_events(answer_response.text)[1]
        application.dependency_overrides[get_workspace_context] = lambda: foreign.context
        foreign_response = client.get(
            f"/api/v1/evidence-spans/{citation_event['payload']['evidence_span_id']}"
        )
        unknown_response = client.get(f"/api/v1/evidence-spans/{uuid4()}")

    assert answer_response.status_code == 200
    assert foreign_response.status_code == unknown_response.status_code == 404
    foreign_error = foreign_response.json()["error"]
    unknown_error = unknown_response.json()["error"]
    assert foreign_error["code"] == unknown_error["code"] == "NOT_FOUND"
    assert foreign_error["message"] == unknown_error["message"]
    assert UUID(foreign_error["request_id"])
    assert UUID(unknown_error["request_id"])
    assert foreign_response.headers["x-request-id"] == foreign_error["request_id"]
    assert unknown_response.headers["x-request-id"] == unknown_error["request_id"]
    assert foreign_error["request_id"] != unknown_error["request_id"]
    assert citation_event["payload"]["evidence_span_id"] not in foreign_response.text
