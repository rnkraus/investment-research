"""Unit tests for the validated research HTTP and SSE boundary."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_answer_research_request_service,
    get_workspace_context,
)
from investment_research.main import create_app
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.application import AnswerResearchRequestService
from investment_research.modules.research.contracts import (
    ResearchRetrievalInput,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    StatementSupportStatus,
    StatementType,
)
from investment_research.settings import ApiSettings

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
RUN_ID = UUID("10000000-0000-4000-8000-000000000002")
ANSWER_ID = UUID("10000000-0000-4000-8000-000000000003")
STATEMENT_ID = UUID("10000000-0000-4000-8000-000000000004")
SPAN_ID = UUID("10000000-0000-4000-8000-000000000005")
RELATIONSHIP_ID = UUID("10000000-0000-4000-8000-000000000006")
VALIDATION_ID = UUID("10000000-0000-4000-8000-000000000007")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000008")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000009")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000010")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = CREATED_AT + timedelta(seconds=1)
QUESTION = "What was Northstar Grid Analytics' FY2025 revenue?"
STATEMENT = "Northstar Grid Analytics reported FY2025 revenue of $184.2 million."
EVIDENCE_TEXT = "FY2025 revenue was $184.2 million."
INSUFFICIENT_MESSAGE = "The archive does not contain sufficient evidence."
LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


class FakeAnswerResearchRequestService:
    """Return one configured terminal result and record trusted application input."""

    def __init__(self) -> None:
        self.result = make_result()
        self.error: Exception | None = None
        self.calls: list[tuple[WorkspaceContext, ResearchRetrievalInput]] = []

    def answer(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
    ) -> ValidatedAnswerResult:
        """Record the call before returning or raising the configured outcome."""

        self.calls.append((context, retrieval_input))
        if self.error is not None:
            raise self.error
        return self.result


def make_context() -> WorkspaceContext:
    """Return the trusted server-owned workspace used by route tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_result(*, state: AnswerState = AnswerState.STREAMED) -> ValidatedAnswerResult:
    """Build one valid terminal aggregate for the selected public outcome."""

    if state is AnswerState.STREAMED:
        coverage = CoverageStatus.COMPLETE
        rendered_text = STATEMENT
    elif state is AnswerState.INSUFFICIENT:
        coverage = CoverageStatus.INSUFFICIENT
        rendered_text = INSUFFICIENT_MESSAGE
    else:
        coverage = None
        rendered_text = None

    answer = Answer(
        id=ANSWER_ID,
        workspace_id=WORKSPACE_ID,
        research_request_id=REQUEST_ID,
        retrieval_run_id=RUN_ID,
        version_number=1,
        state=state,
        coverage_status=coverage,
        generator_version="deterministic-fixture-v1",
        generation_config_version="m0.0-generation-v1",
        prompt_version="m0.0-prompt-v1",
        rendered_text=rendered_text,
        created_at=CREATED_AT,
        completed_at=COMPLETED_AT,
    )
    validation = AnswerValidation(
        id=VALIDATION_ID,
        workspace_id=WORKSPACE_ID,
        answer_id=ANSWER_ID,
        rule=ValidationRule.COVERAGE,
        passed=state is not AnswerState.REJECTED,
        reason="private validation detail" if state is AnswerState.REJECTED else None,
        validation_config_version="m0.0-evidence-v1",
        validated_at=COMPLETED_AT,
    )
    if state is not AnswerState.STREAMED:
        return ValidatedAnswerResult(
            answer=answer,
            statements=(),
            evidence_spans=(),
            statement_evidence=(),
            validations=(validation,),
        )

    statement = AnswerStatement(
        id=STATEMENT_ID,
        workspace_id=WORKSPACE_ID,
        answer_id=ANSWER_ID,
        ordinal=0,
        text=STATEMENT,
        statement_type=StatementType.ARCHIVE_DERIVED,
        support_status=StatementSupportStatus.VALIDATED,
        created_at=COMPLETED_AT,
    )
    span = EvidenceSpan(
        id=SPAN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        page_number=2,
        start_character=0,
        end_character=len(EVIDENCE_TEXT),
        exact_text=EVIDENCE_TEXT,
        text_sha256=sha256(EVIDENCE_TEXT.encode()).hexdigest(),
        created_at=COMPLETED_AT,
    )
    relationship = StatementEvidence(
        id=RELATIONSHIP_ID,
        workspace_id=WORKSPACE_ID,
        answer_statement_id=STATEMENT_ID,
        evidence_span_id=SPAN_ID,
        relationship=EvidenceRelationship.SUPPORTS,
        display_order=0,
        created_at=COMPLETED_AT,
    )
    return ValidatedAnswerResult(
        answer=answer,
        statements=(statement,),
        evidence_spans=(span,),
        statement_evidence=(relationship,),
        validations=(validation,),
    )


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode the simple event and JSON data fields emitted by the route."""

    decoded: list[dict[str, Any]] = []
    for frame in body.strip().split("\n\n"):
        lines = frame.splitlines()
        event_name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        decoded.append({"event_name": event_name, "payload": payload})
    return decoded


@pytest.fixture
def research_client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, FakeAnswerResearchRequestService]]:
    """Run the real route with controlled service and workspace dependencies."""

    application = create_app(
        ApiSettings.model_validate(
            {
                "environment": "test",
                "database_url": LOCAL_DATABASE_URL,
                "object_storage_root": tmp_path / "objects",
            }
        )
    )
    fake_service = FakeAnswerResearchRequestService()
    application.dependency_overrides[get_answer_research_request_service] = lambda: cast(
        AnswerResearchRequestService,
        fake_service,
    )
    application.dependency_overrides[get_workspace_context] = make_context

    with TestClient(application) as client:
        yield client, fake_service


def test_research_route_streams_only_validated_statement_and_citation(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client

    response = client.post(
        "/api/v1/research-requests",
        json={"question": QUESTION, "context": {"document_ids": [str(DOCUMENT_ID)]}},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    events = parse_server_sent_events(response.text)
    assert [event["event_name"] for event in events] == [
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]
    assert events[0]["payload"]["text"] == STATEMENT
    assert events[1]["payload"]["evidence_span_id"] == str(SPAN_ID)
    assert events[2]["payload"]["coverage"] == "COMPLETE"
    assert all(event["payload"]["research_request_id"] == str(REQUEST_ID) for event in events)
    assert "workspace" not in response.text
    assert EVIDENCE_TEXT not in response.text
    assert "validation" not in response.text
    assert service.calls == [
        (
            make_context(),
            ResearchRetrievalInput(question=QUESTION, document_ids=(DOCUMENT_ID,)),
        )
    ]


def test_research_route_streams_explicit_insufficient_result_without_content(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client
    service.result = make_result(state=AnswerState.INSUFFICIENT)

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["answer.insufficient"]
    assert events[0]["payload"]["coverage"] == "INSUFFICIENT"
    assert events[0]["payload"]["message"] == INSUFFICIENT_MESSAGE
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text


def test_research_route_hides_rejected_validation_details(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client
    service.result = make_result(state=AnswerState.REJECTED)

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["research.failed"]
    assert events[0]["payload"]["error"]["code"] == "EVIDENCE_VALIDATION_FAILED"
    assert events[0]["payload"]["retriable"] is False
    assert "private validation detail" not in response.text
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text


def test_research_route_translates_database_failure_without_internal_details(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client
    service.error = SQLAlchemyError("private database detail")

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private database detail" not in response.text


def test_research_route_rejects_client_workspace_scope_before_service_call(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client

    response = client.post(
        "/api/v1/research-requests",
        json={"question": QUESTION, "workspace_id": str(WORKSPACE_ID)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "workspace_id" not in response.text
    assert service.calls == []
