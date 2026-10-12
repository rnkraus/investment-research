"""Unit tests for the validated research HTTP and SSE boundary."""

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from threading import Event
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from investment_research.api.dependencies import (
    get_answer_research_request_service,
    get_workspace_context,
)
from investment_research.api.routes.research import stream_research_events
from investment_research.infrastructure.logging import configure_logging
from investment_research.main import create_app
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    ResearchProviderUnavailableError,
)
from investment_research.modules.research.citable_unit_contracts import (
    CitableUnitLimitDetails,
    CitableUnitLimitError,
)
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationSource,
    ResearchModel,
    ResearchProgressStage,
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
LOCAL_REDIS_URL = "redis://:synthetic-secret@127.0.0.1:6379/0"


class FakeAnswerResearchRequestService:
    """Return one configured terminal result and record trusted application input."""

    def __init__(self) -> None:
        self.result = make_result()
        self.error: Exception | None = None
        self.progress_stages: tuple[ResearchProgressStage, ...] = ()
        self.selected_context: AnswerGenerationInput | None = None
        self.calls: list[tuple[WorkspaceContext, ResearchRetrievalInput]] = []
        self.model_services: dict[tuple[ResearchModel, ResearchModel], object] | None = None

    def answer(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
        *,
        progress: Callable[[UUID, ResearchProgressStage], None] | None = None,
        on_context: Callable[[AnswerGenerationInput], None] | None = None,
    ) -> ValidatedAnswerResult:
        """Record the call before returning or raising the configured outcome."""

        self.calls.append((context, retrieval_input))
        if on_context is not None and self.selected_context is not None:
            on_context(self.selected_context)
        if progress is not None:
            for stage in self.progress_stages:
                progress(REQUEST_ID, stage)
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
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        source_start_character=None,
        source_end_character=None,
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
                "redis_url": LOCAL_REDIS_URL,
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


@pytest.mark.parametrize("fails", [False, True])
def test_research_route_exposes_selected_context_separately_from_answer(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService], fails: bool
) -> None:
    client, service = research_client
    service.selected_context = AnswerGenerationInput(
        research_request_id=REQUEST_ID,
        question=QUESTION,
        sources=(
            AnswerGenerationSource(
                retrieval_candidate_id=RUN_ID,
                chunk_id=BLOCK_ID,
                document_id=DOCUMENT_ID,
                document_version_id=VERSION_ID,
                structure_block_id=BLOCK_ID,
                source_locator_kind=SourceLocatorKind.PDF_PAGE,
                page_number=2,
                text=EVIDENCE_TEXT,
            ),
        ),
    )
    if fails:
        service.error = ResearchProviderUnavailableError(REQUEST_ID, retriable=True)

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert events[0]["event_name"] == "research.context"
    assert events[0]["payload"]["sources"] == [
        service.selected_context.sources[0].model_dump(mode="json")
    ]
    assert events[-1]["event_name"] == ("research.failed" if fails else "answer.completed")
    assert "workspace_id" not in events[0]["payload"]
    assert "question" not in events[0]["payload"]
    if fails:
        assert not any(event["event_name"] == "answer.statement" for event in events)


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
    assert response.headers["cache-control"] == "private, no-store, no-transform"
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


def test_research_route_passes_independent_model_choices(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client

    response = client.post(
        "/api/v1/research-requests",
        json={
            "question": QUESTION,
            "generation_model": ResearchModel.LUNA,
            "support_model": ResearchModel.TERRA,
        },
    )

    assert response.status_code == 200
    assert service.calls == [
        (
            make_context(),
            ResearchRetrievalInput(
                question=QUESTION,
                generation_model=ResearchModel.LUNA,
                support_model=ResearchModel.TERRA,
            ),
        )
    ]


def test_research_route_rejects_unknown_model_before_starting_research(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client

    response = client.post(
        "/api/v1/research-requests",
        json={"question": QUESTION, "generation_model": "unsupported-model"},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_research_models_endpoint_reports_actual_runtime_availability(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client

    deterministic_response = client.get("/api/v1/research-requests/models")
    assert deterministic_response.status_code == 200
    assert deterministic_response.json() == {
        "provider_enabled": False,
        "generation_models": [],
        "support_models": [],
    }

    service.model_services = {
        (answer, support): object() for answer in ResearchModel for support in ResearchModel
    }
    provider_response = client.get("/api/v1/research-requests/models")

    assert provider_response.status_code == 200
    assert provider_response.json() == {
        "provider_enabled": True,
        "generation_models": [model.value for model in ResearchModel],
        "support_models": [model.value for model in ResearchModel],
    }


def test_research_route_streams_progress_before_validated_answer(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client
    service.progress_stages = (
        "EMBEDDING_QUESTION",
        "SEARCHING_SOURCES",
        "SELECTING_PASSAGES",
        "GENERATING_ANSWER",
        "VALIDATING_EVIDENCE",
        "SAVING_RESULT",
    )

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert [event["event_name"] for event in events] == [
        *(["research.progress"] * 6),
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]
    assert [event["payload"]["stage"] for event in events[:6]] == list(service.progress_stages)
    assert all(event["payload"]["research_request_id"] == str(REQUEST_ID) for event in events)
    assert STATEMENT not in str(events[:6])


def test_research_progress_arrives_while_service_is_still_running() -> None:
    release = Event()

    class WaitingService(FakeAnswerResearchRequestService):
        def answer(
            self,
            context: WorkspaceContext,
            retrieval_input: ResearchRetrievalInput,
            *,
            progress: Callable[[UUID, ResearchProgressStage], None] | None = None,
            on_context: Callable[[AnswerGenerationInput], None] | None = None,
        ) -> ValidatedAnswerResult:
            assert progress is not None
            progress(REQUEST_ID, "GENERATING_ANSWER")
            assert release.wait(timeout=2)
            return self.result

    async def collect() -> tuple[bytes, list[bytes]]:
        stream = stream_research_events(
            cast(AnswerResearchRequestService, WaitingService()),
            make_context(),
            ResearchRetrievalInput(question=QUESTION),
            None,
        )
        try:
            first = await asyncio.wait_for(anext(stream), timeout=1)
            assert not release.is_set()
            release.set()
            remaining = [frame async for frame in stream]
            return first, remaining
        finally:
            release.set()
            await stream.aclose()

    first, remaining = asyncio.run(collect())

    assert b"research.progress" in first
    assert b"GENERATING_ANSWER" in first
    assert b"answer.statement" in remaining[0]


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


@pytest.mark.parametrize("retriable", [True, False])
def test_research_route_streams_one_safe_provider_failure(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
    retriable: bool,
) -> None:
    client, service = research_client
    service.error = ResearchProviderUnavailableError(REQUEST_ID, retriable=retriable)
    service.progress_stages = ("GENERATING_ANSWER",)

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == [
        "research.progress",
        "research.failed",
    ]
    assert events[1]["payload"]["research_request_id"] == str(REQUEST_ID)
    assert events[1]["payload"]["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert events[1]["payload"]["error"]["message"] == "research provider is unavailable"
    assert events[1]["payload"]["error"]["request_id"] is not None
    assert events[1]["payload"]["retriable"] is retriable
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text


@pytest.mark.parametrize(
    "details",
    [
        CitableUnitLimitDetails(kind="source_count", observed=9, limit=8),
        CitableUnitLimitDetails(kind="context_characters", observed=24_001, limit=24_000),
        CitableUnitLimitDetails(kind="unit_count", observed=257, limit=256),
        CitableUnitLimitDetails(kind="input_metadata", observed=16_001, limit=16_000),
        None,
    ],
)
def test_research_route_streams_safe_context_limit_and_logs_only_counters(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
    details: CitableUnitLimitDetails | None,
) -> None:
    client, service = research_client
    service.error = CitableUnitLimitError("private-source-marker", details=details)
    service.progress_stages = ("GENERATING_ANSWER",)
    log_stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=log_stream)

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["research.progress", "research.failed"]
    failure = events[-1]["payload"]
    assert failure["research_request_id"] == str(REQUEST_ID)
    assert failure["error"]["code"] == "RESEARCH_CONTEXT_LIMIT"
    assert failure["error"]["message"] == (
        "Retrieved sources exceed the current research limits. "
        "Try a more focused question or select a single document."
    )
    assert failure["error"]["request_id"] is not None
    assert failure["retriable"] is False
    assert "answer.statement" not in response.text
    assert "answer.citation" not in response.text
    records = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    limits = [record for record in records if record["event"] == "research.context_limit.exceeded"]
    assert len(limits) == 1
    assert limits[0]["request_id"] == failure["error"]["request_id"]
    if details is not None:
        assert limits[0]["limit_kind"] == details.kind
        assert limits[0]["observed_value"] == details.observed
        assert limits[0]["limit_value"] == details.limit
    else:
        assert "limit_kind" not in limits[0]
    for content in (response.text, log_stream.getvalue()):
        assert "private-source-marker" not in content
        assert QUESTION not in content


def test_research_route_translates_database_failure_without_internal_details(
    research_client: tuple[TestClient, FakeAnswerResearchRequestService],
) -> None:
    client, service = research_client
    service.error = SQLAlchemyError("private database detail")

    response = client.post("/api/v1/research-requests", json={"question": QUESTION})

    assert response.status_code == 200
    events = parse_server_sent_events(response.text)
    assert [event["event_name"] for event in events] == ["research.failed"]
    assert events[0]["payload"]["error"]["code"] == "INTERNAL_ERROR"
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
