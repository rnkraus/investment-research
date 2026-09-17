"""Application services for research retrieval and validated answer generation."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.contracts import (
    EvidenceValidationDecision,
    EvidenceValidationProfile,
    ValidationFinding,
)
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.evidence.ports import EvidenceRepository, StatementSupportEvaluator
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationProfile,
    AnswerGenerationSource,
    ResearchRetrievalInput,
    ResearchRetrievalResult,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    CoverageStatus,
    ResearchRequest,
    ResearchScopeError,
    StatementSupportStatus,
    StatementType,
    TemporalMode,
)
from investment_research.modules.research.ports import (
    AnswerGenerator,
    AnswerValidationUnitOfWorkFactory,
    ResearchRepository,
    ResearchRetrievalUnitOfWorkFactory,
)
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


class GenerateValidatedAnswerService:
    """Generate outside a transaction, then validate and persist one answer atomically."""

    def __init__(
        self,
        *,
        unit_of_work_factory: AnswerValidationUnitOfWorkFactory,
        answer_generator: AnswerGenerator,
        support_evaluator: StatementSupportEvaluator,
        generation_profile: AnswerGenerationProfile,
        validation_profile: EvidenceValidationProfile,
        insufficient_message: str,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        if not insufficient_message.strip():
            raise ValueError("insufficient_message must contain a non-whitespace character")
        self._unit_of_work_factory = unit_of_work_factory
        self._answer_generator = answer_generator
        self._support_evaluator = support_evaluator
        self._generation_profile = generation_profile
        self._validation_profile = validation_profile
        self._insufficient_message = insufficient_message
        self._clock = clock
        self._identifier_factory = identifier_factory

    def generate(
        self,
        context: WorkspaceContext,
        retrieval_result: ResearchRetrievalResult,
    ) -> ValidatedAnswerResult:
        """Persist only the terminal aggregate derived from a complete validation decision."""

        if retrieval_result.request.workspace_id != context.workspace_id:
            raise ResearchScopeError("research result is outside the active workspace")

        created_at = self._aware_utc_now()
        answer = Answer(
            id=self._identifier_factory(),
            workspace_id=context.workspace_id,
            research_request_id=retrieval_result.request.id,
            version_number=1,
            state=AnswerState.REQUESTED,
            generator_version=self._generation_profile.generator_version,
            generation_config_version=self._generation_profile.generation_config_version,
            prompt_version=self._generation_profile.prompt_version,
            created_at=created_at,
        )
        generation_input = self._build_generation_input(retrieval_result)
        generated_answer = self._answer_generator.generate(generation_input)

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_answer(context, answer)
            answer = self._advance_answer(
                unit_of_work.research,
                context,
                answer,
                state=AnswerState.RETRIEVING,
            )
            answer = self._advance_answer(
                unit_of_work.research,
                context,
                answer,
                state=AnswerState.GENERATING,
                retrieval_run_id=retrieval_result.retrieval_run.id,
            )
            answer = self._advance_answer(
                unit_of_work.research,
                context,
                answer,
                state=AnswerState.VALIDATING,
            )
            decision = ValidateGeneratedAnswerService(
                source_reader=unit_of_work.evidence_sources,
                support_evaluator=self._support_evaluator,
            ).validate(
                context,
                retrieval_result.retrieval_run.id,
                generated_answer,
            )
            completed_at = self._aware_utc_now()
            result = self._build_terminal_result(
                answer=answer,
                decision=decision,
                completed_at=completed_at,
            )
            if not unit_of_work.research.update_answer(context, result.answer):
                raise RuntimeError("answer disappeared before its terminal transition")
            if result.answer.state is AnswerState.STREAMED:
                unit_of_work.research.add_statements(
                    context,
                    result.answer,
                    result.statements,
                )
                result = self._reuse_canonical_evidence_spans(
                    context,
                    unit_of_work.evidence,
                    result,
                )
                unit_of_work.flush()
                for statement in result.statements:
                    relationships = tuple(
                        relationship
                        for relationship in result.statement_evidence
                        if relationship.answer_statement_id == statement.id
                    )
                    unit_of_work.evidence.add_statement_evidence(
                        context,
                        statement.id,
                        relationships,
                    )
            unit_of_work.evidence.add_answer_validations(
                context,
                result.answer.id,
                result.validations,
            )

        return result

    @staticmethod
    def _reuse_canonical_evidence_spans(
        context: WorkspaceContext,
        repository: EvidenceRepository,
        result: ValidatedAnswerResult,
    ) -> ValidatedAnswerResult:
        """Replace proposed span IDs with their canonical persisted identities."""

        canonical_by_proposed_id = {
            span.id: repository.get_or_add_span(context, span) for span in result.evidence_spans
        }
        relationships = tuple(
            relationship.model_copy(
                update={
                    "evidence_span_id": canonical_by_proposed_id[relationship.evidence_span_id].id
                }
            )
            for relationship in result.statement_evidence
        )
        return ValidatedAnswerResult(
            answer=result.answer,
            statements=result.statements,
            evidence_spans=tuple(canonical_by_proposed_id.values()),
            statement_evidence=relationships,
            validations=result.validations,
        )

    @staticmethod
    def _build_generation_input(
        retrieval_result: ResearchRetrievalResult,
    ) -> AnswerGenerationInput:
        """Expose only the bounded ordered candidate data required by generation."""

        return AnswerGenerationInput(
            research_request_id=retrieval_result.request.id,
            question=retrieval_result.request.question,
            sources=tuple(
                AnswerGenerationSource(
                    retrieval_candidate_id=candidate.id,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    document_version_id=hit.document_version_id,
                    structure_block_id=hit.structure_block_id,
                    page_number=hit.page_number,
                    text=hit.text,
                )
                for candidate, hit in zip(
                    retrieval_result.candidates,
                    retrieval_result.hits,
                    strict=True,
                )
            ),
        )

    def _build_terminal_result(
        self,
        *,
        answer: Answer,
        decision: EvidenceValidationDecision,
        completed_at: datetime,
    ) -> ValidatedAnswerResult:
        """Map one safe decision to canonical domain records with server-generated IDs."""

        terminal_answer = self._terminal_answer(answer, decision, completed_at)
        statements: list[AnswerStatement] = []
        spans_by_key: dict[tuple[UUID, UUID, int, int], EvidenceSpan] = {}
        relationships: list[StatementEvidence] = []
        statement_ids_by_ordinal: dict[int, UUID] = {}

        for proposal in decision.statements:
            statement_id = self._identifier_factory()
            statement_ids_by_ordinal[proposal.ordinal] = statement_id
            statements.append(
                AnswerStatement(
                    id=statement_id,
                    workspace_id=answer.workspace_id,
                    answer_id=answer.id,
                    ordinal=proposal.ordinal,
                    text=proposal.text,
                    statement_type=StatementType.ARCHIVE_DERIVED,
                    support_status=StatementSupportStatus.VALIDATED,
                    created_at=completed_at,
                )
            )
            for display_order, reference in enumerate(proposal.evidence):
                span_key = (
                    reference.document_version_id,
                    reference.structure_block_id,
                    reference.start_character,
                    reference.end_character,
                )
                span = spans_by_key.get(span_key)
                if span is None:
                    span = EvidenceSpan(
                        id=self._identifier_factory(),
                        workspace_id=answer.workspace_id,
                        document_version_id=reference.document_version_id,
                        structure_block_id=reference.structure_block_id,
                        source_locator_kind=SourceLocatorKind.PDF_PAGE,
                        page_number=reference.page_number,
                        source_start_character=None,
                        source_end_character=None,
                        start_character=reference.start_character,
                        end_character=reference.end_character,
                        exact_text=reference.exact_text,
                        text_sha256=reference.text_sha256,
                        created_at=completed_at,
                    )
                    spans_by_key[span_key] = span
                relationships.append(
                    StatementEvidence(
                        id=self._identifier_factory(),
                        workspace_id=answer.workspace_id,
                        answer_statement_id=statement_id,
                        evidence_span_id=span.id,
                        relationship=EvidenceRelationship.SUPPORTS,
                        display_order=display_order,
                        created_at=completed_at,
                    )
                )

        validations = self._build_validation_records(
            answer_id=answer.id,
            workspace_id=answer.workspace_id,
            decision=decision,
            statement_ids_by_ordinal=statement_ids_by_ordinal,
            validated_at=completed_at,
        )
        return ValidatedAnswerResult(
            answer=terminal_answer,
            statements=tuple(statements),
            evidence_spans=tuple(spans_by_key.values()),
            statement_evidence=tuple(relationships),
            validations=validations,
        )

    def _terminal_answer(
        self,
        answer: Answer,
        decision: EvidenceValidationDecision,
        completed_at: datetime,
    ) -> Answer:
        """Derive terminal user-visible content only from the validated decision."""

        if not decision.accepted:
            state = AnswerState.REJECTED
            coverage_status = None
            rendered_text = None
        elif decision.coverage_status is CoverageStatus.INSUFFICIENT:
            state = AnswerState.INSUFFICIENT
            coverage_status = CoverageStatus.INSUFFICIENT
            rendered_text = self._insufficient_message
        else:
            state = AnswerState.STREAMED
            coverage_status = decision.coverage_status
            rendered_text = "\n\n".join(statement.text for statement in decision.statements)
        return Answer.model_validate(
            {
                **answer.model_dump(),
                "state": state,
                "coverage_status": coverage_status,
                "rendered_text": rendered_text,
                "completed_at": completed_at,
            }
        )

    def _build_validation_records(
        self,
        *,
        answer_id: UUID,
        workspace_id: UUID,
        decision: EvidenceValidationDecision,
        statement_ids_by_ordinal: dict[int, UUID],
        validated_at: datetime,
    ) -> tuple[AnswerValidation, ...]:
        """Map findings to statement records or safe answer-level rejection summaries."""

        findings = decision.findings
        if not decision.accepted:
            findings = self._aggregate_rejected_findings(findings)
        return tuple(
            AnswerValidation(
                id=self._identifier_factory(),
                workspace_id=workspace_id,
                answer_id=answer_id,
                answer_statement_id=(
                    None
                    if finding.statement_ordinal is None
                    else statement_ids_by_ordinal[finding.statement_ordinal]
                ),
                rule=finding.rule,
                passed=finding.passed,
                reason=finding.reason,
                validation_config_version=self._validation_profile.validation_config_version,
                validated_at=validated_at,
            )
            for finding in findings
        )

    @staticmethod
    def _aggregate_rejected_findings(
        findings: tuple[ValidationFinding, ...],
    ) -> tuple[ValidationFinding, ...]:
        """Collapse rejected statement rules to unique answer-level safe results."""

        aggregated: list[ValidationFinding] = []
        for rule in ValidationRule:
            rule_findings = tuple(finding for finding in findings if finding.rule is rule)
            if not rule_findings:
                continue
            passed = all(finding.passed for finding in rule_findings)
            reason = next(
                (finding.reason for finding in rule_findings if not finding.passed),
                None,
            )
            aggregated.append(
                ValidationFinding(
                    rule=rule,
                    passed=passed,
                    reason=reason,
                )
            )
        return tuple(aggregated)

    @staticmethod
    def _advance_answer(
        repository: ResearchRepository,
        context: WorkspaceContext,
        answer: Answer,
        *,
        state: AnswerState,
        retrieval_run_id: UUID | None = None,
    ) -> Answer:
        """Create and persist exactly one permitted lifecycle snapshot."""

        advanced = Answer.model_validate(
            {
                **answer.model_dump(),
                "state": state,
                "retrieval_run_id": (
                    answer.retrieval_run_id if retrieval_run_id is None else retrieval_run_id
                ),
            }
        )
        if not repository.update_answer(context, advanced):
            raise RuntimeError("answer disappeared during its lifecycle")
        return advanced

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("research clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AnswerResearchRequestService:
    """Run retrieval and validated answer persistence as one application use case."""

    retrieval_service: RetrieveResearchService
    generation_service: GenerateValidatedAnswerService

    def answer(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
    ) -> ValidatedAnswerResult:
        """Return only the validated terminal result of one research request."""

        retrieval_result = self.retrieval_service.retrieve(context, retrieval_input)
        return self.generation_service.generate(context, retrieval_result)
