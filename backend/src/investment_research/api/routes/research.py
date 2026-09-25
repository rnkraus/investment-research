"""HTTP endpoint for validated evidence-grounded research answers."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import UUID, uuid4

from fastapi import APIRouter, status
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from investment_research.api.contracts import (
    AnswerCitationEvent,
    AnswerCompletedEvent,
    AnswerInsufficientEvent,
    AnswerStatementEvent,
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    ResearchFailedEvent,
    ResearchModelOptions,
    ResearchProgressEvent,
    ResearchRequestInput,
    ResearchStreamEvent,
)
from investment_research.api.contracts import CoverageStatus as ApiCoverageStatus
from investment_research.api.contracts import (
    EvidenceRelationship as ApiEvidenceRelationship,
)
from investment_research.api.dependencies import (
    AnswerResearchRequestServiceDependency,
    WorkspaceContextDependency,
)
from investment_research.infrastructure.logging import current_request_id
from investment_research.modules.research.application import ResearchProviderUnavailableError
from investment_research.modules.research.contracts import (
    ResearchModel,
    ResearchProgressStage,
    ResearchRetrievalInput,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import AnswerState

router = APIRouter(prefix="/api/v1/research-requests", tags=["research"])
logger = logging.getLogger(__name__)


@router.get("/models", response_model=ResearchModelOptions)
def get_research_models(
    service: AnswerResearchRequestServiceDependency,
) -> ResearchModelOptions:
    """Advertise only model choices backed by the active provider runtime."""

    model_services = service.model_services
    return ResearchModelOptions(
        provider_enabled=model_services is not None,
        generation_models=(tuple(ResearchModel) if model_services is not None else ()),
        support_models=(tuple(ResearchModel) if model_services is not None else ()),
    )


def build_research_stream_events(
    result: ValidatedAnswerResult,
) -> tuple[ResearchStreamEvent, ...]:
    """Map one validated terminal aggregate to safe public stream events."""

    request_id = result.answer.research_request_id
    if result.answer.state is AnswerState.INSUFFICIENT:
        if result.answer.rendered_text is None:
            raise RuntimeError("insufficient answer is missing its safe message")
        return (
            AnswerInsufficientEvent(
                research_request_id=request_id,
                message=result.answer.rendered_text,
            ),
        )

    if result.answer.state is AnswerState.REJECTED:
        return (
            ResearchFailedEvent(
                research_request_id=request_id,
                error=ErrorDetail(
                    code=ErrorCode.EVIDENCE_VALIDATION_FAILED,
                    message="answer failed evidence validation",
                ),
                retriable=False,
            ),
        )

    if result.answer.state is not AnswerState.STREAMED:
        raise RuntimeError("validated answer result has no public terminal mapping")
    if result.answer.coverage_status is None:
        raise RuntimeError("streamed answer is missing coverage")

    events: list[ResearchStreamEvent] = []
    relationships_by_statement = {
        statement.id: sorted(
            (
                relationship
                for relationship in result.statement_evidence
                if relationship.answer_statement_id == statement.id
            ),
            key=lambda relationship: relationship.display_order,
        )
        for statement in result.statements
    }
    for statement in sorted(result.statements, key=lambda item: item.ordinal):
        events.append(
            AnswerStatementEvent(
                research_request_id=request_id,
                statement_id=statement.id,
                order=statement.ordinal,
                text=statement.text,
            )
        )
        events.extend(
            AnswerCitationEvent(
                research_request_id=request_id,
                statement_id=statement.id,
                evidence_span_id=relationship.evidence_span_id,
                relationship=ApiEvidenceRelationship(relationship.relationship.value),
                display_order=relationship.display_order,
            )
            for relationship in relationships_by_statement[statement.id]
        )

    events.append(
        AnswerCompletedEvent(
            research_request_id=request_id,
            coverage=ApiCoverageStatus(result.answer.coverage_status.value),
        )
    )
    return tuple(events)


def encode_server_sent_event(event: ResearchStreamEvent) -> bytes:
    """Serialize one typed event into a UTF-8 server-sent-event frame."""

    return f"event: {event.event}\ndata: {event.model_dump_json()}\n\n".encode()


async def stream_research_events(
    service: AnswerResearchRequestServiceDependency,
    context: WorkspaceContextDependency,
    retrieval_input: ResearchRetrievalInput,
    http_request_id: UUID | None,
) -> AsyncGenerator[bytes]:
    """Deliver real progress while bounded synchronous research runs in a worker thread."""

    loop = asyncio.get_running_loop()
    events: asyncio.Queue[ResearchStreamEvent | None] = asyncio.Queue()
    research_request_id: UUID | None = None

    def publish_progress(request_id: UUID, stage: ResearchProgressStage) -> None:
        nonlocal research_request_id
        research_request_id = request_id
        # The client may disconnect while a bounded provider call is still running.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(
                events.put_nowait,
                ResearchProgressEvent(research_request_id=request_id, stage=stage),
            )

    async def run_research() -> None:
        try:
            result = await run_in_threadpool(
                service.answer,
                context,
                retrieval_input,
                progress=publish_progress,
            )
            terminal_events = build_research_stream_events(result)
        except ResearchProviderUnavailableError as error:
            terminal_events = (
                ResearchFailedEvent(
                    research_request_id=error.research_request_id,
                    error=ErrorDetail(
                        code=ErrorCode.PROVIDER_UNAVAILABLE,
                        message="research provider is unavailable",
                        request_id=http_request_id,
                    ),
                    retriable=error.retriable,
                ),
            )
        except Exception as error:
            # An SSE response has already started; report a safe terminal event.
            logger.error(
                "research.stream.failed",
                extra={"exception_type": type(error).__name__, "request_id": http_request_id},
            )
            terminal_events = (
                ResearchFailedEvent(
                    research_request_id=research_request_id or uuid4(),
                    error=ErrorDetail(
                        code=ErrorCode.INTERNAL_ERROR,
                        message="research request could not be completed",
                        request_id=http_request_id,
                    ),
                    retriable=False,
                ),
            )
        for event in terminal_events:
            await events.put(event)
        await events.put(None)

    async with asyncio.TaskGroup() as group:
        group.create_task(run_research())
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=10)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if event is None:
                break
            yield encode_server_sent_event(event)


@router.post(
    "",
    response_class=StreamingResponse,
    responses={
        status.HTTP_200_OK: {
            "content": {
                "text/event-stream": {
                    "schema": {"$ref": "#/components/schemas/ResearchStreamEvent"},
                }
            },
            "description": "Validated research answer events",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)
def answer_research_request(
    request: ResearchRequestInput,
    service: AnswerResearchRequestServiceDependency,
    context: WorkspaceContextDependency,
) -> StreamingResponse:
    """Start a live stream without exposing content before complete validation."""

    retrieval_input = ResearchRetrievalInput(
        question=request.question,
        document_ids=request.context.document_ids,
        temporal_mode=request.context.temporal_mode,
        cutoff_at=request.context.cutoff_at,
        generation_model=request.generation_model,
        support_model=request.support_model,
    )
    return StreamingResponse(
        stream_research_events(service, context, retrieval_input, current_request_id()),
        media_type="text/event-stream",
        headers={
            "cache-control": "private, no-store, no-transform",
            "x-content-type-options": "nosniff",
        },
    )
