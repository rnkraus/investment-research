"""Application services for research retrieval and validated answer generation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import sleep
from uuid import UUID, uuid4

from investment_research.modules.evidence.application import ValidateGeneratedAnswerService
from investment_research.modules.evidence.contracts import (
    CanonicalEvidenceSource,
    EvidenceValidationDecision,
    EvidenceValidationProfile,
    StatementSupportDecision,
    StatementSupportResult,
    ValidationFinding,
)
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceRelationship,
    EvidenceSpan,
    StatementEvidence,
    ValidationRule,
)
from investment_research.modules.evidence.ports import (
    CanonicalEvidenceSourceReader,
    EvidenceRepository,
    StatementSupportEvaluator,
    StatementSupportProviderConfigurationError,
    StatementSupportProviderContractError,
    StatementSupportProviderError,
    StatementSupportProviderRefusalError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderOperationAttempt,
    ProviderUsageCostExceededReservationError,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderUsage,
)
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationProfile,
    AnswerGenerationSource,
    CanonicalCandidateSource,
    ResearchRetrievalInput,
    ResearchRetrievalResult,
    UnvalidatedGeneratedAnswer,
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
    AnswerGenerationProviderConfigurationError,
    AnswerGenerationProviderContractError,
    AnswerGenerationProviderError,
    AnswerGenerationProviderRefusalError,
    AnswerGenerationProviderTransientError,
    AnswerGenerator,
    AnswerValidationUnitOfWorkFactory,
    ResearchRepository,
    ResearchRetrievalUnitOfWorkFactory,
)
from investment_research.modules.search.application import (
    EmbeddingResponseContractError,
    build_lexical_search_query,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    M1_PROVIDER_MAX_ATTEMPTS,
    M1_PROVIDER_RETRY_DELAY_SECONDS,
    DenseQualificationRequest,
    DenseRetrievalProfile,
    DenseRetrievalQuery,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    LexicalSearchHit,
    LexicalSearchProfile,
    LexicalSearchQuery,
    M1SelectionProfile,
    M1SelectionResult,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.ports import (
    DenseRetriever,
    EmbeddingProvider,
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderError,
    SearchProviderTransientError,
)
from investment_research.modules.search.selection import select_m1_context

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]
type Sleeper = Callable[[float], None]
type CanonicalRetrievalHit = LexicalSearchHit | QualifiedDenseHit
type SupportKey = tuple[str, tuple[str, ...]]


class ResearchProviderUnavailableError(RuntimeError):
    """Safe application failure for one persisted request's provider operation."""

    def __init__(self, research_request_id: UUID, *, retriable: bool) -> None:
        super().__init__("research provider is unavailable")
        self.research_request_id = research_request_id
        self.retriable = retriable


@dataclass(frozen=True, slots=True)
class _SnapshotEvidenceSourceReader:
    """Serve one detached, workspace-scoped snapshot during provider evaluation."""

    workspace_id: UUID
    retrieval_run_id: UUID
    sources: dict[UUID, CanonicalEvidenceSource | None]

    def get_source(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        retrieval_candidate_id: UUID,
    ) -> CanonicalEvidenceSource | None:
        if context.workspace_id != self.workspace_id or retrieval_run_id != self.retrieval_run_id:
            return None
        return self.sources.get(retrieval_candidate_id)


class _RecordingStatementSupportEvaluator:
    """Call the provider outside the answer transaction and retain typed decisions."""

    def __init__(self, evaluator: StatementSupportEvaluator) -> None:
        self._evaluator = evaluator
        self.results: dict[SupportKey, StatementSupportResult] = {}

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        key = (statement, tuple(evidence_texts))
        result = self._evaluator.evaluate(statement, evidence_texts)
        self.results[key] = result
        return result


@dataclass(frozen=True, slots=True)
class _RecordedStatementSupportEvaluator:
    """Fail closed when final canonical evidence lacks a matching paid decision."""

    results: dict[SupportKey, StatementSupportResult]

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        result = self.results.get((statement, tuple(evidence_texts)))
        if result is not None:
            return result
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=False),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


@dataclass(frozen=True, slots=True)
class QueryEmbeddingAccountingProfile:
    """Provider identity and reviewed unit pricing for M1 query embeddings."""

    provider: str
    model: str
    cost_policy: ProviderCostPolicy


@dataclass(frozen=True, slots=True)
class AnswerGenerationAccountingProfile:
    """Provider identity, unit bounds, and reviewed pricing for answer generation."""

    provider: str
    model: str
    input_overhead_units: int
    maximum_output_units: int
    cost_policy: ProviderCostPolicy

    def __post_init__(self) -> None:
        """Require positive integer bounds without accepting booleans as integers."""

        for value in (self.input_overhead_units, self.maximum_output_units):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("answer-generation accounting bounds must be positive integers")


@dataclass(frozen=True, slots=True)
class StatementSupportAccountingProfile:
    """Provider identity, unit bounds, configuration, and pricing for statement support."""

    provider: str
    model: str
    configuration_version: str
    input_overhead_units: int
    maximum_output_units: int
    cost_policy: ProviderCostPolicy

    def __post_init__(self) -> None:
        """Require positive integer bounds without accepting booleans as integers."""

        for value in (self.input_overhead_units, self.maximum_output_units):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("statement-support accounting bounds must be positive integers")


@dataclass(frozen=True, slots=True)
class M1QueryRetrievalDependencies:
    """Complete provider and policy bundle required by M1 dual-lane retrieval."""

    embedding_provider: EmbeddingProvider
    dense_retriever: DenseRetriever
    dense_profile: DenseRetrievalProfile
    selection_profile: M1SelectionProfile
    provider_operation_executor: ExecuteProviderOperationService
    embedding_accounting: QueryEmbeddingAccountingProfile
    retry_delay_seconds: float = M1_PROVIDER_RETRY_DELAY_SECONDS
    sleeper: Sleeper = sleep

    def __post_init__(self) -> None:
        """Keep injected retry timing inside the frozen M1 policy."""

        if not 0 <= self.retry_delay_seconds <= M1_PROVIDER_RETRY_DELAY_SECONDS:
            raise ValueError("query provider retry delay must be between 0 and 0.25 seconds")


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
    """Create one request and atomically persist its qualified retrieval result."""

    def __init__(
        self,
        *,
        unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory,
        search_profile: LexicalSearchProfile,
        m1_query_retrieval: M1QueryRetrievalDependencies | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._search_profile = search_profile
        self._m1_query_retrieval = m1_query_retrieval
        self._clock = clock
        self._identifier_factory = identifier_factory

    def retrieve(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
    ) -> ResearchRetrievalResult:
        """Run the configured bounded retrieval mode and persist one coherent result."""

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

        if self._m1_query_retrieval is not None:
            return self._retrieve_dual_lane(
                context=context,
                request=request,
                query=query,
                retrieval_run_id=retrieval_run_id,
                started_at=started_at,
            )

        return self._retrieve_lexical(
            context=context,
            request=request,
            query=query,
            retrieval_run_id=retrieval_run_id,
            started_at=started_at,
        )

    def _retrieve_lexical(
        self,
        *,
        context: WorkspaceContext,
        request: ResearchRequest,
        query: LexicalSearchQuery,
        retrieval_run_id: UUID,
        started_at: datetime,
    ) -> ResearchRetrievalResult:
        """Retain the credential-free lexical regression path."""

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
                mode=RetrievalMode.LEXICAL,
                lexical_config_version=query.lexical_config_version,
                selector_config_version=LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
                lexical_candidate_limit=query.candidate_limit,
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
            sources = tuple(
                CanonicalCandidateSource(
                    retrieval_candidate_id=candidate.id,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    document_version_id=hit.document_version_id,
                    structure_block_id=hit.structure_block_id,
                    source_locator_kind=hit.source_locator_kind,
                    page_number=hit.page_number,
                    text=hit.text,
                )
                for candidate, hit in zip(candidates, hits, strict=True)
            )

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            sources=sources,
        )

    def _retrieve_dual_lane(
        self,
        *,
        context: WorkspaceContext,
        request: ResearchRequest,
        query: LexicalSearchQuery,
        retrieval_run_id: UUID,
        started_at: datetime,
    ) -> ResearchRetrievalResult:
        """Run external query providers first, then commit canonical selection atomically."""
        dependencies = self._m1_query_retrieval
        if dependencies is None:
            raise RuntimeError("M1 query retrieval dependencies are required")

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_request(context, request)

        embedding_request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=request.id,
                    text=request.question,
                    purpose=EmbeddingPurpose.QUERY,
                ),
            ),
            embedding_config_version=dependencies.dense_profile.embedding_config_version,
        )
        embedding_attempt = ProviderOperationAttempt(
            correlation_id=request.id,
            research_request_id=request.id,
            processing_run_id=None,
            operation=ProviderOperationKind.QUERY_EMBEDDING,
            provider=dependencies.embedding_accounting.provider,
            model=dependencies.embedding_accounting.model,
            configuration_version=dependencies.dense_profile.embedding_config_version,
            maximum_usage=ProviderUsage(
                input_units=sum(
                    len(item.text.encode("utf-8")) for item in embedding_request.inputs
                ),
                output_units=0,
            ),
            cost_policy=dependencies.embedding_accounting.cost_policy,
        )
        try:
            embedding_result = self._call_with_retry(
                lambda: dependencies.provider_operation_executor.execute(
                    context,
                    embedding_attempt,
                    lambda: validate_embedding_response(
                        embedding_request,
                        dependencies.embedding_provider.embed(embedding_request),
                    ),
                    lambda result: result.usage,
                    self._classify_query_embedding_error,
                ),
                dependencies,
            )
        except ProviderBudgetExceededError as error:
            raise ResearchProviderUnavailableError(request.id, retriable=False) from error
        except ProviderUsageCostExceededReservationError as error:
            raise ResearchProviderUnavailableError(request.id, retriable=False) from error
        except SearchProviderError as error:
            raise ResearchProviderUnavailableError(
                request.id,
                retriable=isinstance(error, SearchProviderTransientError),
            ) from error
        embedding = embedding_result.vectors[0]
        dense_query = DenseRetrievalQuery(
            query_vector=embedding.values,
            filters=query.filters,
            embedding_config_version=dependencies.dense_profile.embedding_config_version,
            dense_index_config_version=dependencies.dense_profile.dense_index_config_version,
            candidate_limit=dependencies.dense_profile.candidate_limit,
        )
        try:
            provider_hits = self._call_with_retry(
                lambda: tuple(dependencies.dense_retriever.retrieve(context, dense_query)),
                dependencies,
            )
        except SearchProviderError as error:
            raise ResearchProviderUnavailableError(
                request.id,
                retriable=isinstance(error, SearchProviderTransientError),
            ) from error
        qualification_request = DenseQualificationRequest(
            provider_hits=provider_hits,
            filters=query.filters,
            lexical_config_version=query.lexical_config_version,
            embedding_config_version=dense_query.embedding_config_version,
            dense_index_config_version=dense_query.dense_index_config_version,
        )

        with self._unit_of_work_factory() as unit_of_work:
            lexical_hits = tuple(unit_of_work.lexical_search.search(context, query))
            dense_hits = tuple(unit_of_work.dense_qualifier.qualify(context, qualification_request))
            selection = select_m1_context(
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                profile=dependencies.selection_profile,
            )
            completed_at = self._aware_utc_now()
            run = RetrievalRun(
                id=retrieval_run_id,
                workspace_id=context.workspace_id,
                research_request_id=request.id,
                query_text=request.question,
                normalized_query=query.normalized_query,
                filters=query.filters,
                mode=RetrievalMode.DUAL_LANE,
                lexical_config_version=query.lexical_config_version,
                embedding_config_version=dense_query.embedding_config_version,
                dense_index_config_version=dense_query.dense_index_config_version,
                selector_config_version=selection.profile.selector_config_version,
                lexical_candidate_limit=query.candidate_limit,
                dense_candidate_limit=dense_query.candidate_limit,
                context_chunk_limit=selection.profile.context_chunk_limit,
                context_character_limit=selection.profile.context_character_limit,
                started_at=started_at,
                completed_at=completed_at,
            )
            candidates, candidate_by_lane = self._build_dual_lane_candidates(
                context=context,
                run=run,
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                selection=selection,
            )
            unit_of_work.retrieval.add_result(context, run, candidates)
            sources = tuple(
                CanonicalCandidateSource(
                    retrieval_candidate_id=candidate_by_lane[(selected.origin, selected.rank)].id,
                    chunk_id=selected.chunk_id,
                    document_id=selected.document_id,
                    document_version_id=selected.document_version_id,
                    structure_block_id=selected.structure_block_id,
                    source_locator_kind=selected.source_locator_kind,
                    page_number=selected.page_number,
                    text=selected.text,
                )
                for selected in selection.candidates
            )

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            sources=sources,
        )

    def _build_dual_lane_candidates(
        self,
        *,
        context: WorkspaceContext,
        run: RetrievalRun,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
        selection: M1SelectionResult,
    ) -> tuple[
        tuple[RetrievalCandidate, ...],
        dict[tuple[RetrievalOrigin, int], RetrievalCandidate],
    ]:
        """Create all persisted lane candidates and attach final ranks only to selections."""

        selection_rank_by_lane = {
            (candidate.origin, candidate.rank): candidate.selection_rank
            for candidate in selection.candidates
        }
        candidates: list[RetrievalCandidate] = []
        candidate_by_lane: dict[tuple[RetrievalOrigin, int], RetrievalCandidate] = {}
        lanes: tuple[
            tuple[RetrievalOrigin, tuple[CanonicalRetrievalHit, ...]],
            tuple[RetrievalOrigin, tuple[CanonicalRetrievalHit, ...]],
        ] = (
            (RetrievalOrigin.LEXICAL, lexical_hits),
            (RetrievalOrigin.DENSE, dense_hits),
        )
        for origin, hits in lanes:
            for rank, hit in enumerate(hits, start=1):
                candidate = RetrievalCandidate(
                    id=self._identifier_factory(),
                    workspace_id=context.workspace_id,
                    retrieval_run_id=run.id,
                    chunk_id=hit.chunk_id,
                    origin=origin,
                    rank=rank,
                    raw_score=hit.raw_score,
                    selection_rank=selection_rank_by_lane.get((origin, rank)),
                    created_at=run.completed_at,
                )
                candidates.append(candidate)
                candidate_by_lane[(origin, rank)] = candidate
        return tuple(candidates), candidate_by_lane

    @staticmethod
    def _call_with_retry[ResultT](
        operation: Callable[[], ResultT],
        dependencies: M1QueryRetrievalDependencies,
    ) -> ResultT:
        """Retry only classified transient provider failures under the frozen M1 policy."""

        for attempt in range(M1_PROVIDER_MAX_ATTEMPTS):
            try:
                return operation()
            except SearchProviderTransientError:
                if attempt + 1 == M1_PROVIDER_MAX_ATTEMPTS:
                    raise
                dependencies.sleeper(dependencies.retry_delay_seconds)
        raise RuntimeError("query provider retry loop terminated without a result")

    @staticmethod
    def _classify_query_embedding_error(error: Exception) -> ProviderOperationErrorCode:
        """Map query-embedding failures to content-free canonical categories."""

        if isinstance(error, SearchProviderTransientError):
            return ProviderOperationErrorCode.UNAVAILABLE
        if isinstance(error, (SearchProviderContractError, EmbeddingResponseContractError)):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, SearchProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("research clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _AccountedStatementSupportEvaluator:
    """Apply the canonical provider-operation boundary to one support evaluator."""

    context: WorkspaceContext
    research_request_id: UUID
    evaluator: StatementSupportEvaluator
    executor: ExecuteProviderOperationService
    accounting: StatementSupportAccountingProfile

    def evaluate(
        self,
        statement: str,
        evidence_texts: Sequence[str],
    ) -> StatementSupportResult:
        """Reserve, call, and finalize one independently attributable support decision."""

        maximum_input_units = (
            self.accounting.input_overhead_units
            + len(statement.encode("utf-8"))
            + sum(len(evidence_text.encode("utf-8")) for evidence_text in evidence_texts)
        )
        return self.executor.execute(
            self.context,
            ProviderOperationAttempt(
                correlation_id=self.research_request_id,
                research_request_id=self.research_request_id,
                processing_run_id=None,
                operation=ProviderOperationKind.STATEMENT_SUPPORT,
                provider=self.accounting.provider,
                model=self.accounting.model,
                configuration_version=self.accounting.configuration_version,
                maximum_usage=ProviderUsage(
                    input_units=maximum_input_units,
                    output_units=self.accounting.maximum_output_units,
                ),
                cost_policy=self.accounting.cost_policy,
            ),
            lambda: self.evaluator.evaluate(statement, evidence_texts),
            lambda result: result.usage,
            self._classify_error,
        )

    @staticmethod
    def _classify_error(error: Exception) -> ProviderOperationErrorCode:
        """Map support-provider failures to content-free canonical categories."""

        if isinstance(error, StatementSupportProviderTransientError):
            return ProviderOperationErrorCode.UNAVAILABLE
        if isinstance(error, StatementSupportProviderRefusalError):
            return ProviderOperationErrorCode.REFUSAL
        if isinstance(error, StatementSupportProviderContractError):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, StatementSupportProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE


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
        provider_operation_executor: ExecuteProviderOperationService | None = None,
        generation_accounting: AnswerGenerationAccountingProfile | None = None,
        statement_support_accounting: StatementSupportAccountingProfile | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        if not insufficient_message.strip():
            raise ValueError("insufficient_message must contain a non-whitespace character")
        if provider_operation_executor is None and (
            generation_accounting is not None or statement_support_accounting is not None
        ):
            raise ValueError("provider accounting profiles require an operation executor")
        if provider_operation_executor is not None and (
            generation_accounting is None or statement_support_accounting is None
        ):
            raise ValueError(
                "provider accounting requires generation and statement-support profiles"
            )
        self._unit_of_work_factory = unit_of_work_factory
        self._answer_generator = answer_generator
        self._support_evaluator = support_evaluator
        self._generation_profile = generation_profile
        self._validation_profile = validation_profile
        self._insufficient_message = insufficient_message
        self._provider_operation_executor = provider_operation_executor
        self._generation_accounting = generation_accounting
        self._statement_support_accounting = statement_support_accounting
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
        generated_answer = self._generate_answer(context, generation_input)
        source_snapshot = self._load_source_snapshot(
            context,
            retrieval_result.retrieval_run.id,
            generated_answer,
        )
        support_evaluator = _RecordingStatementSupportEvaluator(
            self._accounted_support_evaluator(context, retrieval_result.request.id)
        )
        initial_decision = ValidateGeneratedAnswerService(
            source_reader=source_snapshot,
            support_evaluator=support_evaluator,
        ).validate(context, retrieval_result.retrieval_run.id, generated_answer)

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
            decision = initial_decision
            if initial_decision.accepted and initial_decision.statements:
                decision = ValidateGeneratedAnswerService(
                    source_reader=unit_of_work.evidence_sources,
                    support_evaluator=_RecordedStatementSupportEvaluator(support_evaluator.results),
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

    def _load_source_snapshot(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
        generated_answer: UnvalidatedGeneratedAnswer,
    ) -> CanonicalEvidenceSourceReader:
        """Read proposed sources in one short transaction before any support call."""

        candidate_ids = {
            reference.retrieval_candidate_id
            for statement in generated_answer.statements
            for reference in statement.evidence
        }
        with self._unit_of_work_factory() as unit_of_work:
            sources = {
                candidate_id: unit_of_work.evidence_sources.get_source(
                    context,
                    retrieval_run_id,
                    candidate_id,
                )
                for candidate_id in candidate_ids
            }
        return _SnapshotEvidenceSourceReader(
            workspace_id=context.workspace_id,
            retrieval_run_id=retrieval_run_id,
            sources=sources,
        )

    def _accounted_support_evaluator(
        self,
        context: WorkspaceContext,
        research_request_id: UUID,
    ) -> StatementSupportEvaluator:
        """Decorate paid support evaluation without changing deterministic M0.0 behavior."""

        executor = self._provider_operation_executor
        accounting = self._statement_support_accounting
        if executor is None or accounting is None:
            return self._support_evaluator
        return _AccountedStatementSupportEvaluator(
            context=context,
            research_request_id=research_request_id,
            evaluator=self._support_evaluator,
            executor=executor,
            accounting=accounting,
        )

    def _generate_answer(
        self,
        context: WorkspaceContext,
        generation_input: AnswerGenerationInput,
    ) -> UnvalidatedGeneratedAnswer:
        """Run deterministic generation directly or one accounted paid-provider call."""

        executor = self._provider_operation_executor
        accounting = self._generation_accounting
        if executor is None or accounting is None:
            return self._answer_generator.generate(generation_input).answer

        maximum_input_units = (
            accounting.input_overhead_units
            + len(generation_input.question.encode("utf-8"))
            + sum(len(source.text.encode("utf-8")) for source in generation_input.sources)
        )
        result = executor.execute(
            context,
            ProviderOperationAttempt(
                correlation_id=generation_input.research_request_id,
                research_request_id=generation_input.research_request_id,
                processing_run_id=None,
                operation=ProviderOperationKind.ANSWER_GENERATION,
                provider=accounting.provider,
                model=accounting.model,
                configuration_version=self._generation_profile.generation_config_version,
                maximum_usage=ProviderUsage(
                    input_units=maximum_input_units,
                    output_units=accounting.maximum_output_units,
                ),
                cost_policy=accounting.cost_policy,
            ),
            lambda: self._answer_generator.generate(generation_input),
            lambda generation_result: generation_result.usage,
            self._classify_answer_generation_error,
        )
        return result.answer

    @staticmethod
    def _classify_answer_generation_error(error: Exception) -> ProviderOperationErrorCode:
        """Map answer-generation failures to content-free canonical categories."""

        if isinstance(error, AnswerGenerationProviderTransientError):
            return ProviderOperationErrorCode.UNAVAILABLE
        if isinstance(error, AnswerGenerationProviderRefusalError):
            return ProviderOperationErrorCode.REFUSAL
        if isinstance(error, AnswerGenerationProviderContractError):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, AnswerGenerationProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE

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
                    retrieval_candidate_id=source.retrieval_candidate_id,
                    chunk_id=source.chunk_id,
                    document_id=source.document_id,
                    document_version_id=source.document_version_id,
                    structure_block_id=source.structure_block_id,
                    source_locator_kind=source.source_locator_kind,
                    page_number=source.page_number,
                    text=source.text,
                )
                for source in retrieval_result.sources
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
                        source_locator_kind=reference.source_locator_kind,
                        page_number=reference.page_number,
                        source_start_character=reference.source_start_character,
                        source_end_character=reference.source_end_character,
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
        try:
            return self.generation_service.generate(context, retrieval_result)
        except (
            AnswerGenerationProviderError,
            StatementSupportProviderError,
            ProviderBudgetExceededError,
            ProviderUsageCostExceededReservationError,
        ) as error:
            retriable = isinstance(
                error,
                (
                    AnswerGenerationProviderTransientError,
                    StatementSupportProviderTransientError,
                ),
            )
            raise ResearchProviderUnavailableError(
                retrieval_result.request.id,
                retriable=retriable,
            ) from error
