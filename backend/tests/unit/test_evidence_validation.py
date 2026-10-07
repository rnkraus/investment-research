"""Unit tests for fail-closed canonical evidence validation."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.modules.documents.domain import (
    DocumentLifecycleState,
    SourceLocatorKind,
)
from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    ContextualStatementSupportInput,
    StatementSupportDecision,
    StatementSupportResult,
    ValidationFinding,
)
from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.evidence.ports import CanonicalEvidenceSourceReader
from investment_research.modules.evidence.support_context import build_contextual_support_input
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import (
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.search.domain import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
CANDIDATE_ID = UUID("10000000-0000-4000-8000-000000000002")
UNKNOWN_CANDIDATE_ID = UUID("10000000-0000-4000-8000-000000000003")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000005")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000006")
OTHER_VERSION_ID = UUID("10000000-0000-4000-8000-000000000007")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000008")
FIXTURE_TIME = datetime(2026, 2, 20, 12, tzinfo=UTC)
EVIDENCE_TEXT = "FY2025 revenue was $184.2 million, up 22.0% from $151.0 million in FY2024."
SOURCE_TEXT = f"Financial performance. {EVIDENCE_TEXT} Outlook remained unchanged."
STATEMENT = (
    "Northstar Grid Analytics reported FY2025 revenue of $184.2 million, "
    "up 22.0% from $151.0 million in FY2024."
)
REFERENCE_START = SOURCE_TEXT.index(EVIDENCE_TEXT)


class RecordingSourceReader:
    """Return one controllable canonical source and record lookup attempts."""

    def __init__(self, source: CanonicalEvidenceSource | None) -> None:
        self.source = source
        self.calls: list[tuple[WorkspaceContext, UUID, UUID]] = []

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        self.calls.append((context, retrieval_run_id, retrieval_candidate_id))
        if retrieval_candidate_id != CANDIDATE_ID:
            return None
        return self.source


def make_context() -> WorkspaceContext:
    """Return the trusted workspace context used by the validator tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_source(**changes: object) -> CanonicalEvidenceSource:
    """Build one fully eligible canonical source with optional controlled changes."""

    source = CanonicalEvidenceSource(
        workspace_id=WORKSPACE_ID,
        retrieval_candidate_id=CANDIDATE_ID,
        retrieval_run_id=RUN_ID,
        origin=RetrievalOrigin.LEXICAL,
        selection_rank=None,
        chunk_id=CHUNK_ID,
        chunk_text=SOURCE_TEXT,
        chunk_set_is_active=True,
        chunk_set_lexical_config_version="english-lexical-v1",
        processing_state=ProcessingState.LEXICAL_READY,
        processing_lexical_config_version="english-lexical-v1",
        processing_embedding_config_version="m0.0-embedding-disabled-v1",
        processing_dense_index_config_version="m0.0-dense-index-disabled-v1",
        document_id=DOCUMENT_ID,
        document_lifecycle_state=DocumentLifecycleState.ACTIVE,
        document_current_version_id=VERSION_ID,
        document_version_id=VERSION_ID,
        version_available_at=FIXTURE_TIME,
        version_captured_at=FIXTURE_TIME,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        structure_block_text=SOURCE_TEXT,
        structure_block_start_character=0,
        structure_block_end_character=len(SOURCE_TEXT),
        retrieval_filters=RetrievalFilters(document_ids=(DOCUMENT_ID,)),
        retrieval_mode=RetrievalMode.LEXICAL,
        retrieval_lexical_config_version="english-lexical-v1",
        retrieval_embedding_config_version=None,
        retrieval_dense_index_config_version=None,
    )
    return source.model_copy(update=changes)


def make_dual_lane_source(**changes: object) -> CanonicalEvidenceSource:
    """Build one selected M1 source with matching canonical index configuration."""

    defaults: dict[str, object] = {
        "selection_rank": 1,
        "processing_state": ProcessingState.SEARCHABLE,
        "processing_embedding_config_version": "openai-embedding-v1",
        "processing_dense_index_config_version": "pinecone-index-v1",
        "retrieval_mode": RetrievalMode.DUAL_LANE,
        "retrieval_embedding_config_version": "openai-embedding-v1",
        "retrieval_dense_index_config_version": "pinecone-index-v1",
    }
    return make_source(**(defaults | changes))


def make_answer(
    *,
    candidate_id: UUID = CANDIDATE_ID,
    statement: str = STATEMENT,
    exact_text: str = EVIDENCE_TEXT,
    start_character: int = REFERENCE_START,
) -> UnvalidatedGeneratedAnswer:
    """Build one complete generated proposal around an exact candidate range."""

    return UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.COMPLETE,
        rendered_text=statement,
        statements=(
            UnvalidatedGeneratedStatement(
                text=statement,
                evidence=(
                    UnvalidatedEvidenceReference(
                        retrieval_candidate_id=candidate_id,
                        start_character=start_character,
                        end_character=start_character + len(exact_text),
                        exact_text=exact_text,
                    ),
                ),
            ),
        ),
    )


def make_service(
    source: CanonicalEvidenceSource | None,
) -> tuple[ValidateGeneratedAnswerService, RecordingSourceReader]:
    """Build the service with independent deterministic source and support adapters."""

    reader = RecordingSourceReader(source)
    source_reader: CanonicalEvidenceSourceReader = reader
    service = ValidateGeneratedAnswerService(
        source_reader=source_reader,
        support_evaluator=DeterministicFixtureStatementSupportEvaluator(
            [FixtureStatementSupportRule(statement=STATEMENT, evidence_text=EVIDENCE_TEXT)]
        ),
    )
    return service, reader


def findings_by_rule(
    decision_rules: tuple[ValidationFinding, ...],
) -> dict[ValidationRule, bool]:
    """Index concrete findings by their unique rule for concise assertions."""

    return {finding.rule: finding.passed for finding in decision_rules}


def test_valid_proposal_returns_only_canonical_exact_evidence() -> None:
    service, reader = make_service(make_source())

    decision = service.validate(make_context(), RUN_ID, make_answer())

    assert decision.accepted is True
    assert decision.coverage_status is CoverageStatus.COMPLETE
    assert len(decision.statements) == 1
    reference = decision.statements[0].evidence[0]
    assert reference.document_version_id == VERSION_ID
    assert reference.structure_block_id == BLOCK_ID
    assert reference.start_character == REFERENCE_START
    assert reference.end_character == reference.start_character + len(EVIDENCE_TEXT)
    assert reference.exact_text == EVIDENCE_TEXT
    assert len(reference.text_sha256) == 64
    assert all(finding.passed for finding in decision.findings)
    assert reader.calls == [(make_context(), RUN_ID, CANDIDATE_ID)]


class ContextualSupportEvaluator:
    """Record the context path and forbid accidental exact-only evaluation."""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.inputs: list[ContextualStatementSupportInput] = []

    def evaluate(self, statement: str, evidence_texts: object) -> StatementSupportResult:
        raise AssertionError("contextual validation must use the contextual port")

    def evaluate_contextual(
        self, request: ContextualStatementSupportInput
    ) -> StatementSupportResult:
        self.inputs.append(request)
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self.supported),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


@pytest.mark.parametrize("supported", [False, True])
def test_contextual_validation_keeps_exact_citations_and_support_decision(supported: bool) -> None:
    evaluator = ContextualSupportEvaluator(supported)
    service = ValidateGeneratedAnswerService(
        source_reader=RecordingSourceReader(make_source()),
        support_evaluator=evaluator,
        support_context_builder=build_contextual_support_input,
    )
    decision = service.validate(make_context(), RUN_ID, make_answer())
    assert decision.accepted is supported
    assert len(evaluator.inputs) == 1
    paired = evaluator.inputs[0].evidence[0]
    assert paired.reference.exact_text == EVIDENCE_TEXT
    assert paired.reference.start_character == REFERENCE_START
    assert paired.attribution_context.text == SOURCE_TEXT
    assert paired.attribution_context.document_version_id == VERSION_ID


@pytest.mark.parametrize(
    "source",
    [
        None,
        make_source(workspace_id=OTHER_WORKSPACE_ID),
        make_source(chunk_set_is_active=False),
        make_source(document_current_version_id=OTHER_VERSION_ID),
        make_source(structure_block_text="altered original"),
    ],
)
def test_contextual_validation_never_calls_provider_for_ineligible_sources(
    source: CanonicalEvidenceSource | None,
) -> None:
    evaluator = ContextualSupportEvaluator()
    service = ValidateGeneratedAnswerService(
        source_reader=RecordingSourceReader(source),
        support_evaluator=evaluator,
        support_context_builder=build_contextual_support_input,
    )
    assert service.validate(make_context(), RUN_ID, make_answer()).accepted is False
    assert evaluator.inputs == []


def test_contextual_validation_requires_an_explicit_contextual_evaluator() -> None:
    with pytest.raises(ValueError):
        ValidateGeneratedAnswerService(
            source_reader=RecordingSourceReader(make_source()),
            support_evaluator=DeterministicFixtureStatementSupportEvaluator([]),
            support_context_builder=build_contextual_support_input,
        )


@pytest.mark.parametrize("origin", [RetrievalOrigin.LEXICAL, RetrievalOrigin.DENSE])
def test_valid_dual_lane_proposal_accepts_selected_searchable_candidate(
    origin: RetrievalOrigin,
) -> None:
    service, _ = make_service(make_dual_lane_source(origin=origin))

    decision = service.validate(make_context(), RUN_ID, make_answer())

    assert decision.accepted is True
    assert all(finding.passed for finding in decision.findings)


def test_text_proposal_derives_absolute_original_range_from_canonical_block() -> None:
    block_start = 120
    service, _ = make_service(
        make_source(
            source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
            page_number=None,
            structure_block_start_character=block_start,
            structure_block_end_character=block_start + len(SOURCE_TEXT),
        )
    )

    decision = service.validate(make_context(), RUN_ID, make_answer())

    reference = decision.statements[0].evidence[0]
    assert reference.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
    assert reference.page_number is None
    assert reference.source_start_character == block_start + REFERENCE_START
    assert reference.source_end_character == block_start + REFERENCE_START + len(EVIDENCE_TEXT)


def test_explicit_insufficient_result_is_safe_without_loading_sources() -> None:
    service, reader = make_service(make_source())
    insufficient = UnvalidatedGeneratedAnswer(
        coverage_status=CoverageStatus.INSUFFICIENT,
        rendered_text="The archive does not contain sufficient evidence.",
        statements=(),
        missing_information="The archive does not contain sufficient evidence.",
    )

    decision = service.validate(make_context(), RUN_ID, insufficient)

    assert decision.accepted is True
    assert decision.coverage_status is CoverageStatus.INSUFFICIENT
    assert decision.statements == ()
    assert decision.findings[0].rule is ValidationRule.COVERAGE
    assert reader.calls == []


def test_generated_statement_rejects_duplicate_candidate_ranges() -> None:
    reference = make_answer().statements[0].evidence[0]

    with pytest.raises(ValidationError, match="evidence references must be unique"):
        UnvalidatedGeneratedStatement(
            text=STATEMENT,
            evidence=(reference, reference),
        )


@pytest.mark.parametrize(
    ("source", "answer", "failed_rule"),
    [
        pytest.param(
            None,
            make_answer(candidate_id=UNKNOWN_CANDIDATE_ID),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="invented-candidate",
        ),
        pytest.param(
            make_source(workspace_id=OTHER_WORKSPACE_ID),
            make_answer(),
            ValidationRule.WORKSPACE_SCOPE,
            id="foreign-workspace",
        ),
        pytest.param(
            make_source(document_current_version_id=OTHER_VERSION_ID),
            make_answer(),
            ValidationRule.ACTIVE_DOCUMENT_VERSION,
            id="stale-version",
        ),
        pytest.param(
            make_source(chunk_set_is_active=False),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="inactive-chunk-set",
        ),
        pytest.param(
            make_source(processing_state=ProcessingState.FAILED),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="failed-processing",
        ),
        pytest.param(
            make_source(processing_lexical_config_version="other-v1"),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="configuration-drift",
        ),
        pytest.param(
            make_dual_lane_source(selection_rank=None),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="unselected-dual-lane-candidate",
        ),
        pytest.param(
            make_dual_lane_source(processing_embedding_config_version="other-v1"),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="embedding-configuration-drift",
        ),
        pytest.param(
            make_dual_lane_source(processing_dense_index_config_version="other-v1"),
            make_answer(),
            ValidationRule.RETRIEVAL_CANDIDATE,
            id="dense-index-configuration-drift",
        ),
        pytest.param(
            make_source(retrieval_filters=RetrievalFilters(document_ids=(UUID(int=999),))),
            make_answer(),
            ValidationRule.TEMPORAL_ELIGIBILITY,
            id="document-filter",
        ),
        pytest.param(
            make_source(
                retrieval_filters=RetrievalFilters(
                    available_at_cutoff=FIXTURE_TIME - timedelta(days=1)
                )
            ),
            make_answer(),
            ValidationRule.TEMPORAL_ELIGIBILITY,
            id="public-cutoff",
        ),
        pytest.param(
            make_source(),
            make_answer(exact_text="FY2025 revenue was $999.9 million."),
            ValidationRule.EXACT_SOURCE_SPAN,
            id="tampered-text",
        ),
        pytest.param(
            make_source(),
            make_answer(statement="Revenue declined sharply."),
            ValidationRule.STATEMENT_SUPPORT,
            id="unsupported-statement",
        ),
    ],
)
def test_invalid_proposal_never_exposes_answer_content(
    source: CanonicalEvidenceSource | None,
    answer: UnvalidatedGeneratedAnswer,
    failed_rule: ValidationRule,
) -> None:
    service, _ = make_service(source)

    decision = service.validate(make_context(), RUN_ID, answer)

    assert decision.accepted is False
    assert decision.coverage_status is None
    assert decision.statements == ()
    assert findings_by_rule(decision.findings)[failed_rule] is False
    assert findings_by_rule(decision.findings)[ValidationRule.COVERAGE] is False
