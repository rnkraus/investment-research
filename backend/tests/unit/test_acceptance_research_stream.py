"""Regression coverage for additive context events in the runtime acceptance tests."""

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_research.api.contracts import ResearchContextEvent
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.research.contracts import AnswerGenerationSource
from tests.acceptance.research_stream import parse_answer_events

REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000002")


def frame(event: str, *, request_id: UUID = REQUEST_ID) -> str:
    payload: dict[str, object] = {"research_request_id": str(request_id)}
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def context_frame(*, document_id: UUID = DOCUMENT_ID) -> str:
    source = AnswerGenerationSource(
        retrieval_candidate_id=uuid4(),
        chunk_id=uuid4(),
        document_id=document_id,
        document_version_id=uuid4(),
        structure_block_id=uuid4(),
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        text="Public synthetic evidence.",
    )
    event = ResearchContextEvent(research_request_id=REQUEST_ID, sources=(source,))
    return f"event: research.context\ndata: {event.model_dump_json()}\n\n"


def test_context_is_validated_separately_from_the_existing_answer_sequence() -> None:
    body = (
        ": keep-alive\n\n"
        + frame("research.progress")
        + context_frame()
        + frame("research.progress")
        + frame("answer.statement")
        + frame("answer.citation")
        + frame("answer.completed")
    )
    events = parse_answer_events(body, document_ids=[DOCUMENT_ID])
    assert [event["event"] for event in events] == [
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]


@pytest.mark.parametrize("with_context", [False, True])
def test_insufficient_evidence_can_end_before_or_after_context_preparation(
    with_context: bool,
) -> None:
    body = (context_frame() if with_context else "") + frame("answer.insufficient")
    events = parse_answer_events(body, document_ids=[DOCUMENT_ID])
    assert [event["event"] for event in events] == ["answer.insufficient"]


@pytest.mark.parametrize("failure", ["foreign", "duplicate", "late", "request", "missing"])
def test_invalid_context_scope_order_or_identity_is_not_hidden(failure: str) -> None:
    context = context_frame()
    answer = frame("answer.completed")
    bodies = {
        "foreign": context_frame(document_id=uuid4()) + answer,
        "duplicate": context + context + answer,
        "late": answer + context,
        "request": context + frame("answer.completed", request_id=uuid4()),
        "missing": answer,
    }
    with pytest.raises(AssertionError):
        parse_answer_events(bodies[failure], document_ids=[DOCUMENT_ID])


def test_malformed_context_is_not_silently_ignored() -> None:
    with pytest.raises(ValidationError):
        parse_answer_events(frame("research.context"), document_ids=[DOCUMENT_ID])


def test_unknown_events_remain_visible_to_the_journey_sequence_assertions() -> None:
    events = parse_answer_events(
        context_frame() + frame("unexpected.event") + frame("answer.completed"),
        document_ids=[DOCUMENT_ID],
    )
    assert [event["event"] for event in events] == ["unexpected.event", "answer.completed"]
