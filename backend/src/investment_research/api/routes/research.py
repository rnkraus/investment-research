"""HTTP endpoint for validated evidence-grounded research answers."""

from collections.abc import Iterator

from fastapi import APIRouter, status
from sqlalchemy.exc import SQLAlchemyError
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
from investment_research.api.errors import ApiError
from investment_research.modules.research.contracts import (
    ResearchRetrievalInput,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import AnswerState

router = APIRouter(prefix="/api/v1/research-requests", tags=["research"])


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


def encode_server_sent_events(
    events: tuple[ResearchStreamEvent, ...],
) -> Iterator[bytes]:
    """Serialize typed events into UTF-8 server-sent-event frames."""

    for event in events:
        yield f"event: {event.event}\ndata: {event.model_dump_json()}\n\n".encode()


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
    """Execute one bounded request and stream only its validated terminal output."""

    try:
        result = service.answer(
            context,
            ResearchRetrievalInput(
                question=request.question,
                document_ids=request.context.document_ids,
                temporal_mode=request.context.temporal_mode,
                cutoff_at=request.context.cutoff_at,
            ),
        )
    except SQLAlchemyError as error:
        raise ApiError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=ErrorCode.INTERNAL_ERROR,
            message="research request could not be completed",
        ) from error

    events = build_research_stream_events(result)
    return StreamingResponse(
        encode_server_sent_events(events),
        media_type="text/event-stream",
        headers={
            "cache-control": "private, no-store",
            "x-content-type-options": "nosniff",
        },
    )
