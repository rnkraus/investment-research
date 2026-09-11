"""PostgreSQL integration tests for atomic research retrieval orchestration."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    SqlAlchemyResearchRetrievalRepositories,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.search.postgresql import PostgresLexicalSearch
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.application import RetrieveResearchService
from investment_research.modules.research.contracts import ResearchRetrievalInput
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.research.ports import (
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
)
from investment_research.modules.search.contracts import LexicalSearchProfile
from investment_research.modules.search.domain import RetrievalCandidate, RetrievalRun
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository
from tests.integration.factories import FIXTURE_TIME, make_research_source, persist_research_source


class FailingRetrievalRepository:
    """Simulate a persistence failure after a real lexical query has executed."""

    def add_result(
        self,
        context: WorkspaceContext,
        run: RetrievalRun,
        candidates: Sequence[RetrievalCandidate],
    ) -> None:
        raise RuntimeError("simulated retrieval persistence failure")

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> RetrievalRun | None:
        raise NotImplementedError

    def list_candidates(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> Sequence[RetrievalCandidate]:
        raise NotImplementedError


def test_service_commits_request_run_and_ranked_candidates_together(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Research Retrieval Service")
    persist_research_source(integration_database_engine, source)
    request_id = uuid4()
    retrieval_run_id = uuid4()
    candidate_id = uuid4()
    identifiers = iter((request_id, retrieval_run_id, candidate_id))
    clock = iter(
        (
            FIXTURE_TIME + timedelta(seconds=10),
            FIXTURE_TIME + timedelta(seconds=11),
        )
    )
    service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(
            create_session_factory(integration_database_engine)
        ),
        search_profile=LexicalSearchProfile(
            lexical_config_version=source.processing_run.lexical_config_version,
            candidate_limit=10,
        ),
        clock=clock.__next__,
        identifier_factory=identifiers.__next__,
    )

    result = service.retrieve(
        source.context,
        ResearchRetrievalInput(
            question="  What was FY2025 REVENUE?  ",
            document_ids=(source.document.id,),
        ),
    )

    assert result.request.id == request_id
    assert result.retrieval_run.id == retrieval_run_id
    assert [candidate.id for candidate in result.candidates] == [candidate_id]
    assert [candidate.chunk_id for candidate in result.candidates] == [source.chunks[0].id]

    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        research_repository = SqlAlchemyResearchRepository(session)
        retrieval_repository = SqlAlchemyRetrievalRepository(session)
        assert research_repository.get_request_by_id(source.context, request_id) == result.request
        assert (
            retrieval_repository.get_run_by_id(source.context, retrieval_run_id)
            == result.retrieval_run
        )
        assert (
            tuple(retrieval_repository.list_candidates(source.context, retrieval_run_id))
            == result.candidates
        )


def test_failure_after_real_search_rolls_back_the_flushed_request(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Research Retrieval Rollback")
    persist_research_source(integration_database_engine, source)
    request_id = uuid4()
    retrieval_run_id = uuid4()
    candidate_id = uuid4()
    identifiers = iter((request_id, retrieval_run_id, candidate_id))
    clock = iter(
        (
            FIXTURE_TIME + timedelta(seconds=10),
            FIXTURE_TIME + timedelta(seconds=11),
        )
    )
    session_factory = create_session_factory(integration_database_engine)

    @contextmanager
    def failing_unit_of_work() -> Iterator[ResearchRetrievalUnitOfWork]:
        with transactional_session(session_factory) as session:
            yield SqlAlchemyResearchRetrievalRepositories(
                research=SqlAlchemyResearchRepository(session),
                lexical_search=PostgresLexicalSearch(session),
                retrieval=FailingRetrievalRepository(),
            )

    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = failing_unit_of_work
    service = RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version=source.processing_run.lexical_config_version,
            candidate_limit=10,
        ),
        clock=clock.__next__,
        identifier_factory=identifiers.__next__,
    )

    with pytest.raises(RuntimeError, match="simulated retrieval persistence failure"):
        service.retrieve(
            source.context,
            ResearchRetrievalInput(
                question="What was FY2025 revenue?",
                document_ids=(source.document.id,),
            ),
        )

    with session_factory() as session:
        assert (
            SqlAlchemyResearchRepository(session).get_request_by_id(
                source.context,
                request_id,
            )
            is None
        )
        assert (
            SqlAlchemyRetrievalRepository(session).get_run_by_id(
                source.context,
                retrieval_run_id,
            )
            is None
        )
