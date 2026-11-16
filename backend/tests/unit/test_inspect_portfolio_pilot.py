"""Exercise portfolio pilot scoring on captured stream events without providers."""

import json
from uuid import UUID

import pytest

from scripts.inspect_portfolio_pilot import (
    PilotInputError,
    SourceVersion,
    inspect_capture,
    request_for,
)
from scripts.score_portfolio_retrieval import Case, Target

DOCUMENT_ID = UUID(int=1)
VERSION_ID = UUID(int=2)
SOURCES = {"I-Q1": SourceVersion(document_id=DOCUMENT_ID, document_version_id=VERSION_ID)}
A09 = Case(
    id="A09",
    kind="answer",
    question="What did the report say?",
    expected="A correctly dated figure.",
    document_scope=("I-Q1",),
    context_targets=(Target(id="target-date", source="I-Q1", pages=(22,), markers=("Jan 31",)),),
)


def event(name: str, **fields: object) -> str:
    return "data: " + json.dumps({"event": name, "research_request_id": str(UUID(int=3)), **fields})


def capture(*, page: int, version: UUID = VERSION_ID) -> str:
    sources = [
        {
            "retrieval_candidate_id": str(UUID(int=4)),
            "chunk_id": str(UUID(int=5)),
            "document_id": str(DOCUMENT_ID),
            "document_version_id": str(version),
            "structure_block_id": str(UUID(int=6)),
            "source_locator_kind": "PDF_PAGE",
            "page_number": page,
            "text": "Jan 31 target date",
        }
    ]
    return "\n\n".join(
        (
            ": heartbeat",
            event("research.context", sources=sources),
            event("answer.statement", statement_id=str(UUID(int=7)), order=0, text="Next January"),
            event(
                "answer.citation",
                statement_id=str(UUID(int=7)),
                evidence_span_id=str(UUID(int=8)),
                relationship="SUPPORTS",
                display_order=0,
            ),
            event("answer.completed", coverage="PARTIAL"),
        )
    )


def test_reports_markers_in_final_context_independently_of_answer_coverage() -> None:
    assert inspect_capture(A09, SOURCES, capture(page=22))["evidence_marker_ranks"] == {
        "target-date": [1],
    }
    result = inspect_capture(A09, SOURCES, capture(page=4))
    assert result["evidence_marker_ranks"] == {"target-date": []}
    assert result["coverage"] == "PARTIAL"
    assert result["citations"] == 1


def test_rejects_context_from_an_unexpected_version() -> None:
    with pytest.raises(PilotInputError, match="unexpected document or version"):
        inspect_capture(A09, SOURCES, capture(page=22, version=UUID(int=99)))


def test_keeps_reviewed_scope_and_standard_search() -> None:
    request = request_for(A09, SOURCES)
    assert request.context.document_ids == (DOCUMENT_ID,)
    assert request.question == A09.question
    assert request.generation_model == request.support_model == "gpt-5.6-terra"
    assert not request.decompose_query and not request.rerank_with_luna
