"""Unit tests for atomic validated-answer generation orchestration."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    EvidenceValidationProfile,
)
from investment_research.modules.evidence.ports import (
    CanonicalEvidenceSourceReader,
    EvidenceRepository,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.application import GenerateValidatedAnswerService
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationProfile,
    ResearchRetrievalResult,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import (
    AnswerState,
    CoverageStatus,
    ResearchRequest,
    TemporalMode,
)
from investment_research.modules.research.ports import (
    AnswerGenerator,
    AnswerValidationUnitOfWork,
    AnswerValidationUnitOfWorkFactory,
    ResearchRepository,
)
from investment_research.modules.search.contracts import LexicalSearchHit
from investment_research.modules.search.domain import (
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalOrigin,
    RetrievalRun,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("10000000-0000-4000-8000-000000000001")
RUN_ID = UUID("10000000-0000-4000-8000-000000000002")
CANDIDATE_ID = UUID("10000000-0000-4000-8000-000000000003")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000005")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000006")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000007")
RETRIEVED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
ANSWER_CREATED_AT = RETRIEVED_AT + timedelta(seconds=1)
ANSWER_COMPLETED_AT = RETRIEVED_AT + timedelta(seconds=2)
QUESTION = "What was Northstar Grid Analytics' FY2025 revenue?"
EVIDENCE_TEXT = "FY2025 revenue was $184.2 million, up 22.0% from FY2024."
SOURCE_TEXT = f"Financial performance. {EVIDENCE_TEXT} Outlook remained unchanged."
STATEMENT = "Northstar Grid Analytics reported FY2025 revenue of $184.2 million."
SAFE_INSUFFICIENT_MESSAGE = "The archive does not contain sufficient evidence."
REFERENCE_START = SOURCE_TEXT.index(EVIDENCE_TEXT)


class StaticAnswerGenerator:
    """Return one controlled proposal and record whether a transaction was already open."""

    def __init__(
        self,
        result: UnvalidatedGeneratedAnswer,
        unit_of_work: RecordingAnswerValidationUnitOfWork,
    ) -> None:
        self._result = result
        self._unit_of_work = unit_of_work
        self.calls: list[AnswerGenerationInput] = []
        self.transaction_was_open = False

    def generate(self, generation_input: AnswerGenerationInput) -> UnvalidatedGeneratedAnswer:
        self.calls.append(generation_input)
        self.transaction_was_open = self._unit_of_work.enter_count > 0
        return self._result


class RecordingSourceReader:
    """Expose one eligible canonical source to the real validation service."""

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        if (
            context.workspace_id != WORKSPACE_ID
            or retrieval_run_id != RUN_ID
            or retrieval_candidate_id != CANDIDATE_ID
        ):
            return None
        return make_canonical_source()


class RecordingAnswerValidationUnitOfWork:
    """Record transaction completion around mocked repositories and a real source reader."""

    def __init__(self) -> None:
        self.research_mock = create_autospec(ResearchRepository, instance=True)
        self.evidence_mock = create_autospec(EvidenceRepository, instance=True)
        self.research_mock.update_answer.return_value = True
        self.research = cast(ResearchRepository, self.research_mock)
        self.evidence = cast(EvidenceRepository, self.evidence_mock)
        self.evidence_sources: CanonicalEvidenceSourceReader = RecordingSourceReader()
        self.enter_count = 0
        self.flush_count = 0
        self.committed = False
        self.rolled_back = False

    def flush(self) -> None:
        """Record the dependency-order boundary without committing anything."""

        self.flush_count += 1

    @contextmanager
    def open(self) -> Iterator[AnswerValidationUnitOfWork]:
        """Model the success and failure semantics of the real transaction manager."""

        self.enter_count += 1
        try:
            yield self
        except BaseException:
            self.rolled_back = True
            raise
        else:
            self.committed = True


def make_context() -> WorkspaceContext:
    """Return the trusted identity used throughout this service test."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_retrieval_result() -> ResearchRetrievalResult:
    """Build one already-persisted retrieval result around the eligible source."""

    request = ResearchRequest(
        id=REQUEST_ID,
        workspace_id=WORKSPACE_ID,
        question=QUESTION,
        temporal_mode=TemporalMode.CURRENT,
        created_at=RETRIEVED_AT,
    )
    run = RetrievalRun(
        id=RUN_ID,
        workspace_id=WORKSPACE_ID,
        research_request_id=REQUEST_ID,
        query_text=QUESTION,
        normalized_query="northstar grid analytics fy2025 revenue",
        filters=RetrievalFilters(),
        lexical_config_version="m0.0-english-lexical-v1",
        candidate_limit=10,
        started_at=RETRIEVED_AT,
        completed_at=RETRIEVED_AT,
    )
    hit = LexicalSearchHit(
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        page_number=2,
        text=SOURCE_TEXT,
        raw_score=0.75,
    )
    candidate = RetrievalCandidate(
        id=CANDIDATE_ID,
        workspace_id=WORKSPACE_ID,
        retrieval_run_id=RUN_ID,
        chunk_id=CHUNK_ID,
        origin=RetrievalOrigin.LEXICAL,
        rank=1,
        raw_score=hit.raw_score,
        created_at=RETRIEVED_AT,
    )
    return ResearchRetrievalResult(
        request=request,
        retrieval_run=run,
        candidates=(candidate,),
        hits=(hit,),
    )


def make_canonical_source() -> CanonicalEvidenceSource:
    """Return canonical state matching the selected retrieval candidate."""

    return CanonicalEvidenceSource(
        workspace_id=WORKSPACE_ID,
        retrieval_candidate_id=CANDIDATE_ID,
        retrieval_run_id=RUN_ID,
        origin=RetrievalOrigin.LEXICAL,
        chunk_id=CHUNK_ID,
        chunk_text=SOURCE_TEXT,
        chunk_set_is_active=True,
        chunk_set_lexical_config_version="m0.0-english-lexical-v1",
        processing_state=ProcessingState.LEXICAL_READY,
        processing_lexical_config_version="m0.0-english-lexical-v1",
        document_id=DOCUMENT_ID,
        document_lifecycle_state=DocumentLifecycleState.ACTIVE,
        document_current_version_id=VERSION_ID,
        document_version_id=VERSION_ID,
        version_available_at=RETRIEVED_AT,
        version_captured_at=RETRIEVED_AT,
        structure_block_id=BLOCK_ID,
        page_number=2,
        structure_block_text=SOURCE_TEXT,
        retrieval_filters=RetrievalFilters(),
        retrieval_lexical_config_version="m0.0-english-lexical-v1",
    )


def make_answer_proposal(*, exact_text: str = EVIDENCE_TEXT) -> UnvalidatedGeneratedAnswer:
    """Build one complete but still untrusted generator proposal."""

    return UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text="Untrusted generator rendering that must not be persisted.",
        statements=(
            UnvalidatedGeneratedStatement(
                text=STATEMENT,
                evidence=(
                    UnvalidatedEvidenceReference(
                        retrieval_candidate_id=CANDIDATE_ID,
                        start_character=REFERENCE_START,
                        end_character=REFERENCE_START + len(exact_text),
                        exact_text=exact_text,
                    ),
                ),
            ),
        ),
    )


def make_service(
    unit_of_work: RecordingAnswerValidationUnitOfWork,
    proposal: UnvalidatedGeneratedAnswer,
) -> tuple[GenerateValidatedAnswerService, StaticAnswerGenerator]:
    """Build a deterministic service and its observable untrusted generator."""

    generator = StaticAnswerGenerator(proposal, unit_of_work)
    generator_port: AnswerGenerator = generator
    unit_of_work_factory: AnswerValidationUnitOfWorkFactory = unit_of_work.open
    clock_values = iter((ANSWER_CREATED_AT, ANSWER_COMPLETED_AT))
    identifier_values = count(start=100)
    service = GenerateValidatedAnswerService(
        unit_of_work_factory=unit_of_work_factory,
        answer_generator=generator_port,
        support_evaluator=DeterministicFixtureStatementSupportEvaluator(
            [FixtureStatementSupportRule(statement=STATEMENT, evidence_text=EVIDENCE_TEXT)]
        ),
        generation_profile=AnswerGenerationProfile(
            generator_version="deterministic-fixture-v1",
            generation_config_version="m0.0-generation-v1",
            prompt_version="m0.0-prompt-v1",
        ),
        validation_profile=EvidenceValidationProfile(validation_config_version="m0.0-evidence-v1"),
        insufficient_message=SAFE_INSUFFICIENT_MESSAGE,
        clock=clock_values.__next__,
        identifier_factory=lambda: UUID(int=next(identifier_values)),
    )
    return service, generator


def test_valid_answer_persists_only_validated_content_in_one_unit_of_work() -> None:
    unit_of_work = RecordingAnswerValidationUnitOfWork()
    service, generator = make_service(unit_of_work, make_answer_proposal())

    result = service.generate(make_context(), make_retrieval_result())

    assert generator.transaction_was_open is False
    assert generator.calls[0].sources[0].text == SOURCE_TEXT
    assert unit_of_work.committed is True
    assert unit_of_work.rolled_back is False
    assert unit_of_work.flush_count == 1
    assert result.answer.state is AnswerState.STREAMED
    assert result.answer.rendered_text == STATEMENT
    assert (
        result.answer.rendered_text != "Untrusted generator rendering that must not be persisted."
    )
    assert len(result.statements) == 1
    assert len(result.evidence_spans) == 1
    assert result.evidence_spans[0].exact_text == EVIDENCE_TEXT
    assert len(result.statement_evidence) == 1
    assert all(validation.passed for validation in result.validations)

    states = [
        call.args[1].state for call in unit_of_work.research_mock.update_answer.call_args_list
    ]
    assert states == [
        AnswerState.RETRIEVING,
        AnswerState.GENERATING,
        AnswerState.VALIDATING,
        AnswerState.STREAMED,
    ]
    unit_of_work.research_mock.add_statements.assert_called_once()
    unit_of_work.evidence_mock.add_span.assert_called_once()
    unit_of_work.evidence_mock.add_statement_evidence.assert_called_once()
    unit_of_work.evidence_mock.add_answer_validations.assert_called_once()


def test_insufficient_answer_uses_application_message_without_evidence_records() -> None:
    unit_of_work = RecordingAnswerValidationUnitOfWork()
    proposal = UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.INSUFFICIENT,
        rendered_text="Untrusted generator explanation.",
        statements=(),
        missing_information="Untrusted missing-information explanation.",
    )
    service, _ = make_service(unit_of_work, proposal)

    result = service.generate(make_context(), make_retrieval_result())

    assert result.answer.state is AnswerState.INSUFFICIENT
    assert result.answer.rendered_text == SAFE_INSUFFICIENT_MESSAGE
    assert result.statements == ()
    assert result.evidence_spans == ()
    assert result.statement_evidence == ()
    unit_of_work.research_mock.add_statements.assert_not_called()
    unit_of_work.evidence_mock.add_span.assert_not_called()
    unit_of_work.evidence_mock.add_statement_evidence.assert_not_called()
    unit_of_work.evidence_mock.add_answer_validations.assert_called_once()


def test_rejected_answer_stores_findings_without_generated_content() -> None:
    unit_of_work = RecordingAnswerValidationUnitOfWork()
    tampered_text = "X" + EVIDENCE_TEXT[1:]
    service, _ = make_service(
        unit_of_work,
        make_answer_proposal(exact_text=tampered_text),
    )

    result = service.generate(make_context(), make_retrieval_result())

    assert result.answer.state is AnswerState.REJECTED
    assert result.answer.rendered_text is None
    assert result.statements == ()
    assert result.evidence_spans == ()
    assert result.statement_evidence == ()
    assert all(validation.answer_statement_id is None for validation in result.validations)
    assert any(not validation.passed for validation in result.validations)
    unit_of_work.research_mock.add_statements.assert_not_called()
    unit_of_work.evidence_mock.add_span.assert_not_called()
    unit_of_work.evidence_mock.add_statement_evidence.assert_not_called()


def test_persistence_failure_leaves_the_unit_of_work_uncommitted() -> None:
    unit_of_work = RecordingAnswerValidationUnitOfWork()
    unit_of_work.evidence_mock.add_answer_validations.side_effect = RuntimeError(
        "validation write failed"
    )
    service, _ = make_service(unit_of_work, make_answer_proposal())

    with pytest.raises(RuntimeError, match="validation write failed"):
        service.generate(make_context(), make_retrieval_result())

    assert unit_of_work.committed is False
    assert unit_of_work.rolled_back is True


def test_result_contract_rejects_a_cross_workspace_aggregate_record() -> None:
    unit_of_work = RecordingAnswerValidationUnitOfWork()
    service, _ = make_service(unit_of_work, make_answer_proposal())
    result = service.generate(make_context(), make_retrieval_result())
    foreign_span = result.evidence_spans[0].model_copy(update={"workspace_id": UUID(int=999)})

    with pytest.raises(ValidationError, match="crosses a workspace boundary"):
        ValidatedAnswerResult.model_validate(
            {
                **result.model_dump(),
                "evidence_spans": (foreign_span,),
            }
        )
