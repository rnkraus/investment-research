"""Application service for fail-closed canonical evidence validation."""

from hashlib import sha256
from uuid import UUID

from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    EvidenceValidationDecision,
    ResolvedEvidenceSpan,
    ValidatedEvidenceReference,
    ValidatedStatementProposal,
    ValidationFinding,
)
from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.evidence.ports import (
    CanonicalEvidenceSourceReader,
    EvidenceReadUnitOfWorkFactory,
    StatementSupportEvaluator,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import (
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.search.domain import RetrievalOrigin


class EvidenceSpanUnavailableError(Exception):
    """Hide whether a citation span is unknown, inaccessible, stale, or unlinked."""


class ResolveEvidenceSpanService:
    """Authorize one exact citation against current canonical archive state."""

    def __init__(self, unit_of_work_factory: EvidenceReadUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def resolve(
        self,
        context: WorkspaceContext,
        evidence_span_id: UUID,
    ) -> ResolvedEvidenceSpan:
        """Return one current validated span or the uniform unavailable outcome."""

        with self._unit_of_work_factory() as unit_of_work:
            source = unit_of_work.evidence_spans.get_span_source(
                context,
                evidence_span_id,
            )

        if (
            source is None
            or source.span.workspace_id != context.workspace_id
            or source.document_lifecycle_state is not DocumentLifecycleState.ACTIVE
            or source.document_current_version_id != source.span.document_version_id
            or not source.linked_to_streamed_answer
        ):
            raise EvidenceSpanUnavailableError("evidence span is unavailable")

        return ResolvedEvidenceSpan(
            span=source.span,
            document_id=source.document_id,
        )


class ValidateGeneratedAnswerService:
    """Recheck every generated claim against canonical scoped source state."""

    def __init__(
        self,
        *,
        source_reader: CanonicalEvidenceSourceReader,
        support_evaluator: StatementSupportEvaluator,
    ) -> None:
        self._source_reader = source_reader
        self._support_evaluator = support_evaluator

    def validate(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        generated_answer: UnvalidatedGeneratedAnswer,
    ) -> EvidenceValidationDecision:
        """Return only content that survives every server-side evidence rule."""

        if generated_answer.coverage_status is CoverageStatus.INSUFFICIENT:
            return EvidenceValidationDecision(
                accepted=True,
                coverage_status=CoverageStatus.INSUFFICIENT,
                statements=(),
                findings=(
                    ValidationFinding(
                        rule=ValidationRule.COVERAGE,
                        passed=True,
                    ),
                ),
            )

        sources: dict[UUID, CanonicalEvidenceSource | None] = {}
        findings: list[ValidationFinding] = []
        validated_statements: list[ValidatedStatementProposal] = []

        for ordinal, statement in enumerate(generated_answer.statements):
            statement_sources: list[CanonicalEvidenceSource | None] = []
            validated_references: list[ValidatedEvidenceReference] = []
            for reference in statement.evidence:
                if reference.retrieval_candidate_id not in sources:
                    sources[reference.retrieval_candidate_id] = self._source_reader.get_source(
                        context,
                        retrieval_run_id,
                        reference.retrieval_candidate_id,
                    )
                source = sources[reference.retrieval_candidate_id]
                statement_sources.append(source)
                validated_reference = self._validate_exact_reference(reference, source)
                if validated_reference is not None:
                    validated_references.append(validated_reference)

            rule_results = self._validate_statement_rules(
                context=context,
                retrieval_run_id=retrieval_run_id,
                statement_text=statement.text,
                sources=statement_sources,
                validated_references=validated_references,
                expected_reference_count=len(statement.evidence),
            )
            findings.extend(
                ValidationFinding(
                    rule=rule,
                    statement_ordinal=ordinal,
                    passed=passed,
                    reason=None if passed else reason,
                )
                for rule, passed, reason in rule_results
            )
            if all(passed for _, passed, _ in rule_results):
                validated_statements.append(
                    ValidatedStatementProposal(
                        ordinal=ordinal,
                        text=statement.text,
                        evidence=tuple(validated_references),
                    )
                )

        all_statements_valid = len(validated_statements) == len(generated_answer.statements)
        findings.append(
            ValidationFinding(
                rule=ValidationRule.COVERAGE,
                passed=all_statements_valid,
                reason=(
                    None if all_statements_valid else "Answer contains unvalidated statements."
                ),
            )
        )
        if not all_statements_valid:
            return EvidenceValidationDecision(
                accepted=False,
                coverage_status=None,
                statements=(),
                findings=tuple(findings),
            )
        return EvidenceValidationDecision(
            accepted=True,
            coverage_status=generated_answer.coverage_status,
            statements=tuple(validated_statements),
            findings=tuple(findings),
        )

    def _validate_statement_rules(
        self,
        *,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        statement_text: str,
        sources: list[CanonicalEvidenceSource | None],
        validated_references: list[ValidatedEvidenceReference],
        expected_reference_count: int,
    ) -> tuple[tuple[ValidationRule, bool, str], ...]:
        """Evaluate each persisted statement-level rule exactly once."""

        source_present = all(source is not None for source in sources)
        workspace_valid = source_present and all(
            source is not None and source.workspace_id == context.workspace_id for source in sources
        )
        candidate_valid = source_present and all(
            source is not None
            and source.retrieval_run_id == retrieval_run_id
            and source.origin is RetrievalOrigin.LEXICAL
            and source.chunk_set_is_active
            and source.processing_state is ProcessingState.LEXICAL_READY
            and source.chunk_set_lexical_config_version == source.retrieval_lexical_config_version
            and source.processing_lexical_config_version == source.retrieval_lexical_config_version
            for source in sources
        )
        active_version_valid = source_present and all(
            source is not None
            and source.document_lifecycle_state is DocumentLifecycleState.ACTIVE
            and source.document_current_version_id == source.document_version_id
            for source in sources
        )
        temporal_valid = source_present and all(
            source is not None and self._is_temporally_eligible(source) for source in sources
        )
        exact_span_valid = len(validated_references) == expected_reference_count
        support_valid = exact_span_valid and self._support_evaluator.supports(
            statement_text,
            tuple(reference.exact_text for reference in validated_references),
        )
        return (
            (
                ValidationRule.WORKSPACE_SCOPE,
                workspace_valid,
                "Evidence source is unavailable in the active workspace.",
            ),
            (
                ValidationRule.RETRIEVAL_CANDIDATE,
                candidate_valid,
                "Evidence is not an eligible candidate from this retrieval run.",
            ),
            (
                ValidationRule.ACTIVE_DOCUMENT_VERSION,
                active_version_valid,
                "Evidence does not resolve to the active document version.",
            ),
            (
                ValidationRule.TEMPORAL_ELIGIBILITY,
                temporal_valid,
                "Evidence is outside the research request's temporal scope.",
            ),
            (
                ValidationRule.EXACT_SOURCE_SPAN,
                exact_span_valid,
                "Proposed evidence does not match the canonical source span.",
            ),
            (
                ValidationRule.STATEMENT_SUPPORT,
                support_valid,
                "Canonical evidence does not support the complete statement.",
            ),
        )

    @staticmethod
    def _validate_exact_reference(
        reference: UnvalidatedEvidenceReference,
        source: CanonicalEvidenceSource | None,
    ) -> ValidatedEvidenceReference | None:
        """Derive an exact immutable block range only from matching canonical text."""

        if source is None or source.chunk_text != source.structure_block_text:
            return None
        if reference.end_character > len(source.chunk_text):
            return None
        if (
            source.chunk_text[reference.start_character : reference.end_character]
            != reference.exact_text
        ):
            return None

        return ValidatedEvidenceReference(
            retrieval_candidate_id=source.retrieval_candidate_id,
            document_id=source.document_id,
            document_version_id=source.document_version_id,
            structure_block_id=source.structure_block_id,
            page_number=source.page_number,
            start_character=reference.start_character,
            end_character=reference.end_character,
            exact_text=reference.exact_text,
            text_sha256=sha256(reference.exact_text.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _is_temporally_eligible(source: CanonicalEvidenceSource) -> bool:
        """Reapply the persisted document and timestamp filters to canonical state."""

        filters = source.retrieval_filters
        if filters.document_ids and source.document_id not in filters.document_ids:
            return False
        if filters.available_at_cutoff is not None and (
            source.version_available_at is None
            or source.version_available_at > filters.available_at_cutoff
        ):
            return False
        return not (
            filters.captured_at_cutoff is not None
            and source.version_captured_at > filters.captured_at_cutoff
        )
