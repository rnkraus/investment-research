"""Application service for atomic research-request retrieval."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.contracts import (
    ResearchRetrievalInput,
    ResearchRetrievalResult,
)
from investment_research.modules.research.domain import ResearchRequest, TemporalMode
from investment_research.modules.research.ports import ResearchRetrievalUnitOfWorkFactory
from investment_research.modules.search.application import build_lexical_search_query
from investment_research.modules.search.contracts import LexicalSearchProfile
from investment_research.modules.search.domain import (
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalOrigin,
    RetrievalRun,
)

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]


def build_effective_retrieval_filters(
    retrieval_input: ResearchRetrievalInput,
) -> RetrievalFilters:
    """Translate a research time mode into the exact predicates Search must execute."""

    available_at_cutoff = None
    captured_at_cutoff = None
    if retrieval_input.temporal_mode is TemporalMode.PUBLIC_KNOWLEDGE:
        available_at_cutoff = retrieval_input.cutoff_at
    elif retrieval_input.temporal_mode is TemporalMode.PERSONAL_ARCHIVE:
        available_at_cutoff = retrieval_input.cutoff_at
        captured_at_cutoff = retrieval_input.cutoff_at

    return RetrievalFilters(
        document_ids=retrieval_input.document_ids,
        available_at_cutoff=available_at_cutoff,
        captured_at_cutoff=captured_at_cutoff,
    )


class RetrieveResearchService:
    """Create one request and atomically persist its lexical retrieval result."""

    def __init__(
        self,
        *,
        unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory,
        search_profile: LexicalSearchProfile,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._search_profile = search_profile
        self._clock = clock
        self._identifier_factory = identifier_factory

    def retrieve(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
    ) -> ResearchRetrievalResult:
        """Run one bounded lexical retrieval inside its canonical write transaction."""

        started_at = self._aware_utc_now()
        request = ResearchRequest(
            id=self._identifier_factory(),
            workspace_id=context.workspace_id,
            question=retrieval_input.question,
            document_ids=retrieval_input.document_ids,
            temporal_mode=retrieval_input.temporal_mode,
            cutoff_at=retrieval_input.cutoff_at,
            created_at=started_at,
        )
        filters = build_effective_retrieval_filters(retrieval_input)
        query = build_lexical_search_query(
            query_text=request.question,
            filters=filters,
            lexical_config_version=self._search_profile.lexical_config_version,
            candidate_limit=self._search_profile.candidate_limit,
        )
        retrieval_run_id = self._identifier_factory()

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_request(context, request)
            hits = tuple(unit_of_work.lexical_search.search(context, query))
            completed_at = self._aware_utc_now()
            run = RetrievalRun(
                id=retrieval_run_id,
                workspace_id=context.workspace_id,
                research_request_id=request.id,
                query_text=request.question,
                normalized_query=query.normalized_query,
                filters=query.filters,
                lexical_config_version=query.lexical_config_version,
                candidate_limit=query.candidate_limit,
                started_at=started_at,
                completed_at=completed_at,
            )
            candidates = tuple(
                RetrievalCandidate(
                    id=self._identifier_factory(),
                    workspace_id=context.workspace_id,
                    retrieval_run_id=run.id,
                    chunk_id=hit.chunk_id,
                    origin=RetrievalOrigin.LEXICAL,
                    rank=rank,
                    raw_score=hit.raw_score,
                    created_at=completed_at,
                )
                for rank, hit in enumerate(hits, start=1)
            )
            unit_of_work.retrieval.add_result(context, run, candidates)

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            hits=hits,
        )

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("research clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)
