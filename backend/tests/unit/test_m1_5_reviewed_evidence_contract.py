"""Exercise canonical evidence rules over the reviewed M1.5 synthetic cases."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from investment_research.modules.documents.domain import (
    DocumentLifecycleState,
    SourceLocatorKind,
)
from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
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
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus

NOW = datetime(2026, 7, 23, tzinfo=UTC)
CASE_IDS = (
    "question:fy2025-revenue",
    "question:fy2026-rollout-constraint",
    "question:fy2026-dividend",
    "question:fy2026-conflicting-constraint",
    "question:fy2026-european-pilot-budget",
    "question:fy2026-battery-cabinet-window",
)


class _SourceReader:
    def __init__(self, sources: Sequence[CanonicalEvidenceSource]) -> None:
        self.sources = {source.retrieval_candidate_id: source for source in sources}

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        source = self.sources.get(retrieval_candidate_id)
        if source is None or source.workspace_id != context.workspace_id:
            return None
        if source.retrieval_run_id != retrieval_run_id:
            return None
        return source


class _FixedSupportEvaluator:
    def __init__(self, *, supported: bool) -> None:
        self.supported = supported
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        self.calls.append((statement, tuple(evidence_texts)))
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self.supported),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


def _case_inputs(
    case_id: str,
) -> tuple[WorkspaceContext, UUID, tuple[CanonicalEvidenceSource, ...], UnvalidatedGeneratedAnswer]:
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    case = next(item for item in manifest.cases if item.id == case_id)
    passage_by_id = {passage.id: passage for passage in manifest.passages}
    document_ids = {document.id: uuid4() for document in manifest.documents}
    version_ids = {document.id: uuid4() for document in manifest.documents}
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    retrieval_run_id = uuid4()
    filters = RetrievalFilters(
        document_ids=tuple(document_ids[document_id] for document_id in case.scope_document_ids)
    )
    sources: list[CanonicalEvidenceSource] = []
    statements: list[UnvalidatedGeneratedStatement] = []
    for group in case.evidence_groups:
        assert case.expected_answer is not None
        passage = passage_by_id[group[0]]
        anchor = passage.source_anchor
        candidate_id = uuid4()
        sources.append(
            CanonicalEvidenceSource(
                workspace_id=context.workspace_id,
                retrieval_candidate_id=candidate_id,
                retrieval_run_id=retrieval_run_id,
                origin=RetrievalOrigin.LEXICAL,
                selection_rank=len(sources) + 1,
                chunk_id=uuid4(),
                chunk_text=passage.text,
                chunk_set_is_active=True,
                chunk_set_lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                processing_state=ProcessingState.SEARCHABLE,
                processing_lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                processing_embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                processing_dense_index_config_version=(
                    M1_PROCESSING_PROFILE.dense_index_config_version
                ),
                document_id=document_ids[passage.document_id],
                document_lifecycle_state=DocumentLifecycleState.ACTIVE,
                document_current_version_id=version_ids[passage.document_id],
                document_version_id=version_ids[passage.document_id],
                version_available_at=NOW,
                version_captured_at=NOW,
                structure_block_id=uuid4(),
                source_locator_kind=SourceLocatorKind(anchor.kind),
                page_number=anchor.page_number,
                structure_block_text=passage.text,
                structure_block_start_character=anchor.start_character,
                structure_block_end_character=anchor.end_character,
                retrieval_filters=filters,
                retrieval_mode=RetrievalMode.DUAL_LANE,
                retrieval_lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
                retrieval_embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                retrieval_dense_index_config_version=(
                    M1_PROCESSING_PROFILE.dense_index_config_version
                ),
            )
        )
        statements.append(
            UnvalidatedGeneratedStatement(
                text=passage.text if len(case.evidence_groups) > 1 else case.expected_answer,
                evidence=(
                    UnvalidatedEvidenceReference(
                        retrieval_candidate_id=candidate_id,
                        start_character=0,
                        end_character=len(passage.text),
                        exact_text=passage.text,
                    ),
                ),
            )
        )
    coverage = CoverageStatus(case.expected_coverage)
    answer = UnvalidatedGeneratedAnswer(
        coverage_status=coverage,
        rendered_text=case.expected_answer or "The archive does not answer this question.",
        statements=tuple(statements),
        missing_information=(
            "No reviewed source provides this answer."
            if coverage is CoverageStatus.INSUFFICIENT
            else None
        ),
    )
    return context, retrieval_run_id, tuple(sources), answer


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_reviewed_cases_pass_canonical_evidence_rules(case_id: str) -> None:
    """Scripted answers prove validation contracts, not generator or retrieval quality."""

    context, run_id, sources, answer = _case_inputs(case_id)
    support = _FixedSupportEvaluator(supported=True)
    decision = ValidateGeneratedAnswerService(
        source_reader=_SourceReader(sources),
        support_evaluator=support,
    ).validate(context, run_id, answer)

    assert decision.accepted
    assert decision.coverage_status is answer.coverage_status
    assert len(decision.statements) == len(answer.statements)
    assert len(support.calls) == len(answer.statements)
    assert all(finding.passed for finding in decision.findings)
    for source, statement in zip(sources, decision.statements, strict=True):
        reference = statement.evidence[0]
        assert reference.exact_text == source.structure_block_text
        if source.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE:
            assert reference.source_start_character == source.structure_block_start_character
            assert reference.source_end_character == source.structure_block_end_character


def test_changed_quote_fails_closed_without_support_call() -> None:
    """A same-length model alteration cannot become an accepted exact citation."""

    context, run_id, sources, answer = _case_inputs("question:fy2026-battery-cabinet-window")
    statement = answer.statements[0]
    reference = statement.evidence[0]
    changed = UnvalidatedEvidenceReference(
        retrieval_candidate_id=reference.retrieval_candidate_id,
        start_character=reference.start_character,
        end_character=reference.end_character,
        exact_text="X" + reference.exact_text[1:],
    )
    altered = answer.model_copy(
        update={
            "statements": (statement.model_copy(update={"evidence": (changed,)}),),
        }
    )
    support = _FixedSupportEvaluator(supported=True)
    decision = ValidateGeneratedAnswerService(
        source_reader=_SourceReader(sources),
        support_evaluator=support,
    ).validate(context, run_id, altered)

    assert not decision.accepted
    assert not support.calls


def test_rejected_support_prevents_answer_acceptance() -> None:
    """Even a byte-exact source quote cannot bypass the support decision."""

    context, run_id, sources, answer = _case_inputs("question:fy2025-revenue")
    support = _FixedSupportEvaluator(supported=False)
    decision = ValidateGeneratedAnswerService(
        source_reader=_SourceReader(sources),
        support_evaluator=support,
    ).validate(context, run_id, answer)

    assert not decision.accepted
    assert len(support.calls) == 1
