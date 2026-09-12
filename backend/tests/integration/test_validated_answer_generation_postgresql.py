"""PostgreSQL integration tests for atomic validated-answer persistence."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    SqlAlchemyAnswerValidationRepositories,
    create_answer_validation_unit_of_work_factory,
)
from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.infrastructure.evidence.postgresql import (
    PostgresCanonicalEvidenceSourceReader,
)
from investment_research.infrastructure.generation.deterministic import (
    DeterministicFixtureAnswerGenerator,
    FixtureAnswerCase,
)
from investment_research.modules.evidence.contracts import EvidenceValidationProfile
from investment_research.modules.evidence.domain import AnswerValidation
from investment_research.modules.evidence.persistence import (
    AnswerValidationRow,
    EvidenceSpanRow,
    SqlAlchemyEvidenceRepository,
    StatementEvidenceRow,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.application import GenerateValidatedAnswerService
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationProfile,
    ResearchRetrievalResult,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import AnswerState, CoverageStatus
from investment_research.modules.research.persistence import (
    AnswerRow,
    AnswerStatementRow,
    SqlAlchemyResearchRepository,
)
from investment_research.modules.research.ports import (
    AnswerGenerator,
    AnswerValidationUnitOfWork,
    AnswerValidationUnitOfWorkFactory,
)
from investment_research.modules.search.contracts import LexicalSearchHit
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    RetrievalResultFixture,
    make_research_source,
    make_retrieval_result,
    persist_research_source,
    persist_retrieval_result,
)

SAFE_INSUFFICIENT_MESSAGE = "The archive does not contain sufficient evidence."


class StaticAnswerGenerator:
    """Return one controlled schema-valid proposal for negative-path tests."""

    def __init__(self, proposal: UnvalidatedGeneratedAnswer) -> None:
        self._proposal = proposal

    def generate(self, generation_input: AnswerGenerationInput) -> UnvalidatedGeneratedAnswer:
        return self._proposal


class FailingValidationRepository(SqlAlchemyEvidenceRepository):
    """Fail after all answer content has been staged in the shared transaction."""

    def add_answer_validations(
        self,
        context: WorkspaceContext,
        answer_id: UUID,
        validations: Sequence[AnswerValidation],
    ) -> None:
        raise RuntimeError("simulated validation persistence failure")


def make_application_result(
    source: ResearchSource,
    retrieval: RetrievalResultFixture,
) -> ResearchRetrievalResult:
    """Restore the application result shape from the persisted integration graph."""

    hits = tuple(
        LexicalSearchHit(
            chunk_id=chunk.id,
            document_id=source.document.id,
            document_version_id=source.version.id,
            structure_block_id=chunk.structure_block_id,
            page_number=source.blocks[index].page_number,
            text=chunk.text,
            raw_score=candidate.raw_score,
        )
        for index, (chunk, candidate) in enumerate(
            zip(source.chunks, retrieval.candidates, strict=True)
        )
    )
    return ResearchRetrievalResult(
        request=source.request,
        retrieval_run=retrieval.run,
        candidates=retrieval.candidates,
        hits=hits,
    )


def make_service(
    source: ResearchSource,
    *,
    generator: AnswerGenerator,
    unit_of_work_factory: AnswerValidationUnitOfWorkFactory,
    identifier_factory: Callable[[], UUID] = uuid4,
) -> GenerateValidatedAnswerService:
    """Build the real orchestration service around deterministic test adapters."""

    evidence_text = source.blocks[0].text
    return GenerateValidatedAnswerService(
        unit_of_work_factory=unit_of_work_factory,
        answer_generator=generator,
        support_evaluator=DeterministicFixtureStatementSupportEvaluator(
            [
                FixtureStatementSupportRule(
                    statement=evidence_text,
                    evidence_text=evidence_text,
                )
            ]
        ),
        generation_profile=AnswerGenerationProfile(
            generator_version="deterministic-fixture-v1",
            generation_config_version="m0.0-generation-v1",
            prompt_version="m0.0-prompt-v1",
        ),
        validation_profile=EvidenceValidationProfile(validation_config_version="m0.0-evidence-v1"),
        insufficient_message=SAFE_INSUFFICIENT_MESSAGE,
        clock=lambda: FIXTURE_TIME + timedelta(seconds=10),
        identifier_factory=identifier_factory,
    )


def make_complete_generator(source: ResearchSource) -> AnswerGenerator:
    """Return the M0.0 generator configured for the first canonical block."""

    evidence_text = source.blocks[0].text
    return DeterministicFixtureAnswerGenerator(
        answerable_case=FixtureAnswerCase(
            question=source.request.question,
            statement=evidence_text,
            evidence_text=evidence_text,
        ),
        insufficient_message=SAFE_INSUFFICIENT_MESSAGE,
    )


def count_answer_records(engine: Engine, workspace_id: UUID) -> tuple[int, int, int, int, int]:
    """Count every answer aggregate table inside one isolated synthetic workspace."""

    with create_session_factory(engine)() as session:
        counts = tuple(
            session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.workspace_id == workspace_id)
            )
            or 0
            for row_type in (
                AnswerRow,
                AnswerStatementRow,
                EvidenceSpanRow,
                StatementEvidenceRow,
                AnswerValidationRow,
            )
        )
    return (counts[0], counts[1], counts[2], counts[3], counts[4])


def prepare_source(engine: Engine, *, label: str) -> tuple[ResearchSource, ResearchRetrievalResult]:
    """Persist one complete canonical source and retrieval result."""

    source = make_research_source(label=label)
    retrieval = make_retrieval_result(source)
    persist_research_source(engine, source)
    persist_retrieval_result(engine, source, retrieval)
    return source, make_application_result(source, retrieval)


def test_validated_answer_round_trips_as_one_complete_aggregate(
    integration_database_engine: Engine,
) -> None:
    source, retrieval_result = prepare_source(
        integration_database_engine,
        label="Validated Answer Round Trip",
    )
    session_factory = create_session_factory(integration_database_engine)
    service = make_service(
        source,
        generator=make_complete_generator(source),
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(session_factory),
    )

    result = service.generate(source.context, retrieval_result)

    assert result.answer.state is AnswerState.STREAMED
    assert count_answer_records(
        integration_database_engine,
        source.context.workspace_id,
    ) == (1, 1, 1, 1, 7)
    with session_factory() as session:
        research_repository = SqlAlchemyResearchRepository(session)
        evidence_repository = SqlAlchemyEvidenceRepository(session)
        assert (
            research_repository.get_answer_by_id(source.context, result.answer.id) == result.answer
        )
        assert (
            tuple(research_repository.list_statements(source.context, result.answer.id))
            == result.statements
        )
        assert (
            evidence_repository.get_span_by_id(source.context, result.evidence_spans[0].id)
            == result.evidence_spans[0]
        )
        assert (
            tuple(
                evidence_repository.list_statement_evidence(
                    source.context,
                    result.statements[0].id,
                )
            )
            == result.statement_evidence
        )
        persisted_validations = evidence_repository.list_answer_validations(
            source.context,
            result.answer.id,
        )
        assert {item.id: item for item in persisted_validations} == {
            item.id: item for item in result.validations
        }


@pytest.mark.parametrize(
    ("proposal", "expected_state", "expected_validation_count"),
    [
        pytest.param(
            UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.INSUFFICIENT,
                rendered_text="Untrusted insufficient rendering.",
                statements=(),
                missing_information="Untrusted missing-information rendering.",
            ),
            AnswerState.INSUFFICIENT,
            1,
            id="insufficient",
        ),
        pytest.param(
            UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.COMPLETE,
                rendered_text="Untrusted rejected rendering.",
                statements=(
                    UnvalidatedGeneratedStatement(
                        text="Unsupported generated statement.",
                        evidence=(
                            UnvalidatedEvidenceReference(
                                retrieval_candidate_id=UUID(int=999),
                                start_character=0,
                                end_character=8,
                                exact_text="Invented",
                            ),
                        ),
                    ),
                ),
            ),
            AnswerState.REJECTED,
            7,
            id="rejected",
        ),
    ],
)
def test_non_streamed_terminal_answers_persist_without_usable_content(
    integration_database_engine: Engine,
    proposal: UnvalidatedGeneratedAnswer,
    expected_state: AnswerState,
    expected_validation_count: int,
) -> None:
    source, retrieval_result = prepare_source(
        integration_database_engine,
        label=f"Validated Answer {expected_state}",
    )
    session_factory = create_session_factory(integration_database_engine)
    generator: AnswerGenerator = StaticAnswerGenerator(proposal)
    service = make_service(
        source,
        generator=generator,
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(session_factory),
    )

    result = service.generate(source.context, retrieval_result)

    assert result.answer.state is expected_state
    assert result.statements == ()
    assert result.evidence_spans == ()
    assert result.statement_evidence == ()
    if expected_state is AnswerState.REJECTED:
        assert result.answer.rendered_text is None
        assert any(not validation.passed for validation in result.validations)
    else:
        assert result.answer.rendered_text == SAFE_INSUFFICIENT_MESSAGE
        assert all(validation.passed for validation in result.validations)
    assert count_answer_records(
        integration_database_engine,
        source.context.workspace_id,
    ) == (1, 0, 0, 0, expected_validation_count)


def test_validation_write_failure_rolls_back_the_complete_answer_aggregate(
    integration_database_engine: Engine,
) -> None:
    source, retrieval_result = prepare_source(
        integration_database_engine,
        label="Validated Answer Rollback",
    )
    session_factory = create_session_factory(integration_database_engine)

    @contextmanager
    def failing_unit_of_work() -> Iterator[AnswerValidationUnitOfWork]:
        with transactional_session(session_factory) as session:
            yield SqlAlchemyAnswerValidationRepositories(
                research=SqlAlchemyResearchRepository(session),
                evidence=FailingValidationRepository(session),
                evidence_sources=PostgresCanonicalEvidenceSourceReader(session),
                _session=session,
            )

    unit_of_work_factory: AnswerValidationUnitOfWorkFactory = failing_unit_of_work
    answer_id = uuid4()
    first_identifier = True

    def make_identifier() -> UUID:
        nonlocal first_identifier
        if first_identifier:
            first_identifier = False
            return answer_id
        return uuid4()

    service = make_service(
        source,
        generator=make_complete_generator(source),
        unit_of_work_factory=unit_of_work_factory,
        identifier_factory=make_identifier,
    )

    with pytest.raises(RuntimeError, match="simulated validation persistence failure"):
        service.generate(source.context, retrieval_result)

    assert count_answer_records(
        integration_database_engine,
        source.context.workspace_id,
    ) == (0, 0, 0, 0, 0)
    with session_factory() as session:
        assert (
            SqlAlchemyResearchRepository(session).get_answer_by_id(
                source.context,
                answer_id,
            )
            is None
        )
