"""Unit tests for atomic research-request retrieval orchestration."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.application import RetrieveResearchService
from investment_research.modules.research.contracts import (
    ResearchRetrievalInput,
    ResearchRetrievalResult,
)
from investment_research.modules.research.domain import TemporalMode
from investment_research.modules.research.ports import (
    ResearchRepository,
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
)
from investment_research.modules.search.contracts import LexicalSearchHit, LexicalSearchProfile
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.ports import LexicalSearch, RetrievalRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000003")
FIRST_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
SECOND_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000005")
REQUEST_ID = UUID("20000000-0000-4000-8000-000000000001")
RUN_ID = UUID("20000000-0000-4000-8000-000000000002")
FIRST_CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000003")
SECOND_CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000004")
STARTED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 2, 20, 12, 0, 1, tzinfo=UTC)
CUTOFF_AT = datetime(2025, 12, 31, tzinfo=UTC)


class RecordingUnitOfWork:
    """Expose mocked Research and Search collaborators through one transaction boundary."""

    def __init__(self, hits: tuple[LexicalSearchHit, ...] = ()) -> None:
        self.research_mock = create_autospec(ResearchRepository, instance=True)
        self.lexical_search_mock = create_autospec(LexicalSearch, instance=True)
        self.retrieval_mock = create_autospec(RetrievalRepository, instance=True)
        self.research = cast(ResearchRepository, self.research_mock)
        self.lexical_search = cast(LexicalSearch, self.lexical_search_mock)
        self.retrieval = cast(RetrievalRepository, self.retrieval_mock)
        self.lexical_search_mock.search.return_value = hits
        self.enter_count = 0

    @contextmanager
    def open(self) -> Iterator[ResearchRetrievalUnitOfWork]:
        """Yield all collaborators as one logical transaction."""

        self.enter_count += 1
        yield self


def make_context() -> WorkspaceContext:
    """Return the trusted identity used throughout the unit tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_hit(*, chunk_id: UUID, raw_score: float, page_number: int) -> LexicalSearchHit:
    """Build one eligible adapter hit."""

    return LexicalSearchHit(
        chunk_id=chunk_id,
        document_id=DOCUMENT_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=page_number,
        text=f"Eligible source text on page {page_number}.",
        raw_score=raw_score,
    )


def make_service(
    unit_of_work: RecordingUnitOfWork,
    *,
    identifiers: tuple[UUID, ...] = (REQUEST_ID, RUN_ID),
) -> RetrieveResearchService:
    """Build one deterministic service around a recording unit of work."""

    identifier_values = iter(identifiers)
    clock_values = iter((STARTED_AT, COMPLETED_AT))
    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = unit_of_work.open
    return RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version="m0.0-english-lexical-v1",
            candidate_limit=10,
        ),
        clock=clock_values.__next__,
        identifier_factory=identifier_values.__next__,
    )


def test_retrieve_preserves_request_and_maps_ordered_hits_to_candidates() -> None:
    hits = (
        make_hit(chunk_id=FIRST_CHUNK_ID, raw_score=0.8, page_number=2),
        make_hit(chunk_id=SECOND_CHUNK_ID, raw_score=0.4, page_number=3),
    )
    unit_of_work = RecordingUnitOfWork(hits)
    service = make_service(
        unit_of_work,
        identifiers=(REQUEST_ID, RUN_ID, FIRST_CANDIDATE_ID, SECOND_CANDIDATE_ID),
    )
    retrieval_input = ResearchRetrievalInput(
        question="  What was FY2025 REVENUE?  ",
        document_ids=(DOCUMENT_ID,),
    )

    result = service.retrieve(make_context(), retrieval_input)

    assert unit_of_work.enter_count == 1
    assert result.request.id == REQUEST_ID
    assert result.request.question == retrieval_input.question
    assert result.retrieval_run.id == RUN_ID
    assert result.retrieval_run.query_text == retrieval_input.question
    assert result.retrieval_run.normalized_query == "what was fy2025 revenue?"
    assert result.retrieval_run.started_at == STARTED_AT
    assert result.retrieval_run.completed_at == COMPLETED_AT
    assert [candidate.id for candidate in result.candidates] == [
        FIRST_CANDIDATE_ID,
        SECOND_CANDIDATE_ID,
    ]
    assert [candidate.rank for candidate in result.candidates] == [1, 2]
    assert [candidate.chunk_id for candidate in result.candidates] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
    ]
    assert [candidate.raw_score for candidate in result.candidates] == [0.8, 0.4]
    assert [source.retrieval_candidate_id for source in result.sources] == [
        FIRST_CANDIDATE_ID,
        SECOND_CANDIDATE_ID,
    ]
    assert [source.chunk_id for source in result.sources] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
    ]
    assert [source.page_number for source in result.sources] == [2, 3]
    assert [source.text for source in result.sources] == [hit.text for hit in hits]

    unit_of_work.research_mock.add_request.assert_called_once_with(make_context(), result.request)
    search_context, query = unit_of_work.lexical_search_mock.search.call_args.args
    assert search_context == make_context()
    assert query.normalized_query == "what was fy2025 revenue?"
    assert query.filters.document_ids == (DOCUMENT_ID,)
    unit_of_work.retrieval_mock.add_result.assert_called_once_with(
        make_context(), result.retrieval_run, result.candidates
    )


@pytest.mark.parametrize(
    ("temporal_mode", "cutoff_at", "expected_available", "expected_captured"),
    [
        (TemporalMode.CURRENT, None, None, None),
        (TemporalMode.PUBLIC_KNOWLEDGE, CUTOFF_AT, CUTOFF_AT, None),
        (TemporalMode.PERSONAL_ARCHIVE, CUTOFF_AT, CUTOFF_AT, CUTOFF_AT),
    ],
)
def test_retrieve_translates_temporal_mode_to_effective_search_filters(
    temporal_mode: TemporalMode,
    cutoff_at: datetime | None,
    expected_available: datetime | None,
    expected_captured: datetime | None,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(unit_of_work)

    service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="What was FY2025 revenue?",
            temporal_mode=temporal_mode,
            cutoff_at=cutoff_at,
        ),
    )

    _, query = unit_of_work.lexical_search_mock.search.call_args.args
    assert query.filters.available_at_cutoff == expected_available
    assert query.filters.captured_at_cutoff == expected_captured


def test_retrieve_persists_an_empty_result_without_inventing_candidates() -> None:
    unit_of_work = RecordingUnitOfWork()
    service = make_service(unit_of_work)

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(question="What dividend was declared?"),
    )

    assert result.sources == ()
    assert result.candidates == ()
    unit_of_work.retrieval_mock.add_result.assert_called_once_with(
        make_context(), result.retrieval_run, ()
    )


def test_retrieval_result_keeps_dual_lane_candidates_but_one_source_per_chunk() -> None:
    unit_of_work = RecordingUnitOfWork(
        (make_hit(chunk_id=FIRST_CHUNK_ID, raw_score=0.8, page_number=2),)
    )
    result = make_service(
        unit_of_work,
        identifiers=(REQUEST_ID, RUN_ID, FIRST_CANDIDATE_ID),
    ).retrieve(
        make_context(),
        ResearchRetrievalInput(question="What was FY2025 revenue?"),
    )
    dense_candidate = result.candidates[0].model_copy(
        update={
            "id": SECOND_CANDIDATE_ID,
            "origin": RetrievalOrigin.DENSE,
            "raw_score": 0.9,
        }
    )

    dual_lane_result = ResearchRetrievalResult(
        request=result.request,
        retrieval_run=result.retrieval_run,
        candidates=(*result.candidates, dense_candidate),
        sources=result.sources,
    )

    assert len(dual_lane_result.candidates) == 2
    assert len(dual_lane_result.sources) == 1


def test_retrieval_result_rejects_source_with_mismatched_chunk() -> None:
    unit_of_work = RecordingUnitOfWork(
        (make_hit(chunk_id=FIRST_CHUNK_ID, raw_score=0.8, page_number=2),)
    )
    result = make_service(
        unit_of_work,
        identifiers=(REQUEST_ID, RUN_ID, FIRST_CANDIDATE_ID),
    ).retrieve(
        make_context(),
        ResearchRetrievalInput(question="What was FY2025 revenue?"),
    )

    with pytest.raises(ValidationError, match="does not match its retrieval candidate"):
        ResearchRetrievalResult(
            request=result.request,
            retrieval_run=result.retrieval_run,
            candidates=result.candidates,
            sources=(result.sources[0].model_copy(update={"chunk_id": SECOND_CHUNK_ID}),),
        )


def test_retrieval_input_rejects_duplicate_or_excessive_document_filters() -> None:
    with pytest.raises(ValidationError, match="document_ids must be unique"):
        ResearchRetrievalInput(
            question="What was FY2025 revenue?",
            document_ids=(DOCUMENT_ID, DOCUMENT_ID),
        )

    with pytest.raises(ValidationError, match="at most 100"):
        ResearchRetrievalInput(
            question="What was FY2025 revenue?",
            document_ids=tuple(UUID(int=value) for value in range(1, 102)),
        )


def test_retrieve_rejects_a_naive_clock_before_opening_a_transaction() -> None:
    unit_of_work = RecordingUnitOfWork()
    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = unit_of_work.open
    service = RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version="m0.0-english-lexical-v1",
            candidate_limit=10,
        ),
        clock=lambda: datetime(2026, 2, 20, 12),
    )

    with pytest.raises(RuntimeError, match="timezone-aware"):
        service.retrieve(
            make_context(),
            ResearchRetrievalInput(question="What was FY2025 revenue?"),
        )

    assert unit_of_work.enter_count == 0
