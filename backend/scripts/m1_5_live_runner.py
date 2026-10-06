"""Evaluate the frozen synthetic corpus through canonical M1 services and injected ports."""

from collections.abc import Sequence
from time import perf_counter_ns
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
    create_provider_operation_unit_of_work_factory,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.generation.openai import M1_ANSWER_GENERATOR_VERSION
from investment_research.infrastructure.openai_models import (
    answer_config_version,
    support_config_version,
)
from investment_research.infrastructure.operations.noop import NoopOperationTracer
from investment_research.modules.evidence.contracts import EvidenceValidationProfile
from investment_research.modules.evidence.domain import ValidationRule
from investment_research.modules.evidence.persistence import SqlAlchemyEvidenceRepository
from investment_research.modules.evidence.ports import (
    StatementSupportEvaluator,
    StatementSupportProviderError,
)
from investment_research.modules.identity.domain import Workspace, WorkspaceType
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import ProviderOperationKind
from investment_research.modules.research.application import (
    AnswerGenerationAccountingProfile,
    GenerateValidatedAnswerService,
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    RetrieveResearchService,
    StatementSupportAccountingProfile,
)
from investment_research.modules.research.contracts import (
    AnswerGenerationProfile,
    ResearchModel,
    ResearchRetrievalInput,
    ValidatedAnswerResult,
)
from investment_research.modules.research.domain import AnswerState, CoverageStatus
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.research.ports import (
    AnswerGenerationProviderError,
    AnswerGenerator,
)
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    DenseRetrievalProfile,
    LexicalSearchProfile,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.ports import EmbeddingProvider, SearchProviderError
from scripts.m1_5_live_diagnostics import ProviderFailureDiagnostic, provider_failure
from scripts.m1_5_live_index import IsolatedEvaluationIndex
from scripts.m1_5_live_providers import LiveProviderCallError, LiveProviderCalls, ProviderCallRecord
from scripts.m1_5_local_output import LocalEvaluationOutput, require_local_terminal
from scripts.map_m1_5_passages import map_citation_passages, map_selected_passages
from scripts.plan_m1_5_live_baseline import (
    LiveBaselineLimitError,
    LiveBaselinePlan,
    build_plan,
    retrieval_policy_for_version,
    selection_profile_for_policy,
)
from scripts.score_m1_5_observations import (
    CaseObservation,
    ObservationSet,
    ProviderUsage,
    ScoreSummary,
    score_observations,
)
from scripts.verify_m1_5_corpus import DEFAULT_MANIFEST, REPOSITORY_ROOT, validate_corpus
from tests.integration.factories import FIXTURE_TIME, ResearchSource
from tests.integration.m1_5 import index_sources, persist_sources, source_for_catalog_document

type FailureReason = Literal["budget", "provider", "rejected", "verification"]
type FailureStage = Literal[
    "preparation",
    "index_visibility",
    "retrieval",
    "passage_mapping",
    "answer_generation_and_validation",
    "persistence_readback",
    "evidence_validation",
    "citation_mapping",
]
type ObservedCoverage = Literal["COMPLETE", "PARTIAL", "CONFLICTING", "INSUFFICIENT"]
OBSERVED_COVERAGE: dict[CoverageStatus, ObservedCoverage] = {
    CoverageStatus.COMPLETE: "COMPLETE",
    CoverageStatus.PARTIAL: "PARTIAL",
    CoverageStatus.CONFLICTING: "CONFLICTING",
    CoverageStatus.INSUFFICIENT: "INSUFFICIENT",
}


class LiveCaseFailure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    case_id: str
    reason: FailureReason
    stage: FailureStage | None = None
    provider_failure: ProviderFailureDiagnostic | None = None
    failed_validation_rules: tuple[ValidationRule, ...] = ()


class LiveBaselineReport(BaseModel):
    """Content-free observations; a successful run is not a quality acceptance decision."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal[1] = 1
    execution_mode: Literal["live_opt_in"] = "live_opt_in"
    admission_plan: LiveBaselinePlan
    observations: ObservationSet
    summary: ScoreSummary
    failures: tuple[LiveCaseFailure, ...]
    preparation_failed: bool
    preparation_elapsed_ms: int
    provider_calls: tuple[ProviderCallRecord, ...]
    preparation_usage: ProviderUsage
    total_usage: ProviderUsage
    reserved_cost_micro_usd: int
    cleanup_status: Literal["PENDING", "VERIFIED", "FAILED"] = "PENDING"
    cleanup_steps: tuple[str, ...] = ()


def summarize_usage(records: Sequence[ProviderCallRecord]) -> ProviderUsage:
    """Unknown usage poisons totals; no attempted calls means a known zero."""

    known = all(
        record.input_tokens is not None
        and record.output_tokens is not None
        and record.estimated_cost_micro_usd is not None
        for record in records
    )
    return ProviderUsage(
        input_tokens=sum(record.input_tokens or 0 for record in records) if known else None,
        output_tokens=sum(record.output_tokens or 0 for record in records) if known else None,
        estimated_cost_micro_usd=(
            sum(record.estimated_cost_micro_usd or 0 for record in records) if known else None
        ),
    )


def _cost_policy(plan: LiveBaselinePlan, role: ProviderOperationKind) -> ProviderCostPolicy:
    limit = next(item for item in plan.role_limits if item.role is role)
    return ProviderCostPolicy(
        input_micro_usd_per_million_units=limit.input_micro_usd_per_million_tokens,
        output_micro_usd_per_million_units=limit.output_micro_usd_per_million_tokens,
    )


def build_services(
    engine: Engine,
    plan: LiveBaselinePlan,
    *,
    embedding: EmbeddingProvider,
    index: IsolatedEvaluationIndex,
    generator: AnswerGenerator,
    support: StatementSupportEvaluator,
) -> tuple[RetrieveResearchService, GenerateValidatedAnswerService]:
    """Compose canonical services with the explicitly planned selector and model pair."""

    selection_profile = selection_profile_for_policy(
        retrieval_policy_for_version(plan.configuration.selector_config_version)
    )
    sessions = create_session_factory(engine)
    operations = create_provider_operation_unit_of_work_factory(sessions)
    executor = ExecuteProviderOperationService(
        reservation_service=ReserveProviderOperationService(
            unit_of_work_factory=operations,
            budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=5_000_000),
        ),
        finalization_service=FinalizeProviderOperationService(unit_of_work_factory=operations),
        tracer=NoopOperationTracer(),
    )
    retrieval = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(sessions),
        search_profile=LexicalSearchProfile(
            lexical_config_version=plan.configuration.lexical_config_version,
            candidate_limit=M1_LANE_CANDIDATE_LIMIT,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=embedding,
            dense_retriever=index,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=plan.configuration.embedding_config_version,
                dense_index_config_version=plan.configuration.dense_index_config_version,
                candidate_limit=M1_LANE_CANDIDATE_LIMIT,
            ),
            selection_profile=selection_profile,
            provider_operation_executor=executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="openai",
                model="text-embedding-3-small",
                cost_policy=_cost_policy(plan, ProviderOperationKind.QUERY_EMBEDDING),
            ),
        ),
    )
    answer_limit = next(
        item for item in plan.role_limits if item.role is ProviderOperationKind.ANSWER_GENERATION
    )
    support_limit = next(
        item for item in plan.role_limits if item.role is ProviderOperationKind.STATEMENT_SUPPORT
    )
    answer_model = ResearchModel(plan.configuration.answer_model)
    support_model = ResearchModel(plan.configuration.support_model)
    generation = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(sessions),
        answer_generator=generator,
        support_evaluator=support,
        generation_profile=AnswerGenerationProfile(
            generator_version=M1_ANSWER_GENERATOR_VERSION,
            generation_config_version=answer_config_version(answer_model),
            prompt_version=plan.configuration.answer_prompt_version,
        ),
        validation_profile=EvidenceValidationProfile(
            validation_config_version=(
                "m1-evidence-validation-v1"
                if support_model is ResearchModel.TERRA
                else "m1-evidence-validation-v1-luna-support"
            )
        ),
        insufficient_message=(
            "The archive does not contain sufficient evidence to answer this question."
        ),
        provider_operation_executor=executor,
        generation_accounting=AnswerGenerationAccountingProfile(
            provider="openai",
            model=answer_model.value,
            input_overhead_units=answer_limit.input_envelope_units,
            maximum_output_units=answer_limit.maximum_output_tokens,
            cost_policy=_cost_policy(plan, ProviderOperationKind.ANSWER_GENERATION),
        ),
        statement_support_accounting=StatementSupportAccountingProfile(
            provider="openai",
            model=support_model.value,
            configuration_version=support_config_version(support_model),
            input_overhead_units=support_limit.input_envelope_units,
            maximum_output_units=support_limit.maximum_output_tokens,
            cost_policy=_cost_policy(plan, ProviderOperationKind.STATEMENT_SUPPORT),
        ),
    )
    return retrieval, generation


def _verify_persistence(
    engine: Engine, source: ResearchSource, result: ValidatedAnswerResult
) -> None:
    """Only credit an answer whose canonical aggregate can be read back intact."""

    with create_session_factory(engine)() as session:
        research = SqlAlchemyResearchRepository(session)
        evidence = SqlAlchemyEvidenceRepository(session)
        if (
            research.get_answer_by_id(source.context, result.answer.id) != result.answer
            or tuple(research.list_statements(source.context, result.answer.id))
            != result.statements
            or set(evidence.list_answer_validations(source.context, result.answer.id))
            != set(result.validations)
            or any(
                evidence.get_span_by_id(source.context, span.id) != span
                for span in result.evidence_spans
            )
        ):
            raise ValueError("evaluation answer readback failed")


def _reason(error: Exception) -> FailureReason:
    if isinstance(error, LiveBaselineLimitError):
        return "budget"
    if isinstance(
        error,
        SearchProviderError
        | AnswerGenerationProviderError
        | StatementSupportProviderError
        | LiveProviderCallError,
    ):
        return "provider"
    return "verification"


def _provider_diagnostic(
    error: Exception, records: Sequence[ProviderCallRecord]
) -> ProviderFailureDiagnostic | None:
    """Prefer the failing call's role; never infer it from a previous successful call."""

    if _reason(error) != "provider":
        return None
    if records and records[-1].failure is not None:
        return records[-1].failure
    return provider_failure(error)


def run_live_baseline(
    engine: Engine,
    plan: LiveBaselinePlan,
    *,
    index: IsolatedEvaluationIndex,
    document_embedding: EmbeddingProvider,
    query_embedding: EmbeddingProvider,
    generator: AnswerGenerator,
    support: StatementSupportEvaluator,
    calls: LiveProviderCalls,
    local_output: LocalEvaluationOutput | None = None,
) -> LiveBaselineReport:
    """Run every reviewed case without authorizing credentials, resources, or cleanup itself."""

    if (
        plan
        != build_plan(
            answer_model=ResearchModel(plan.configuration.answer_model),
            support_model=ResearchModel(plan.configuration.support_model),
            maximum_spend_micro_usd=plan.maximum_spend_micro_usd,
            retrieval_policy=retrieval_policy_for_version(
                plan.configuration.selector_config_version
            ),
        )
        or calls.budget.plan != plan
    ):
        raise LiveBaselineLimitError("the evaluation plan is inconsistent")
    manifest = validate_corpus(DEFAULT_MANIFEST, REPOSITORY_ROOT)
    if local_output is not None:
        require_local_terminal()
    context = index.context
    workspace = Workspace(
        id=context.workspace_id,
        name="M1.5 isolated evaluation",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=FIXTURE_TIME,
    )
    sources: tuple[ResearchSource, ...] = ()
    preparation_failed = False
    preparation_reason: FailureReason = "verification"
    preparation_stage: FailureStage = "preparation"
    preparation_diagnostic: ProviderFailureDiagnostic | None = None
    started = perf_counter_ns()
    try:
        sources = tuple(
            source_for_catalog_document(document, workspace=workspace, context=context)
            for document in manifest.documents
        )
        if sum(len(source.chunks) for source in sources) > plan.maximum_document_vectors:
            raise LiveBaselineLimitError("the document vector limit is exceeded")
        persist_sources(engine, sources)
        index_sources(sources, provider=document_embedding, index=index)
        preparation_stage = "index_visibility"
        index.wait_until_visible()
    except Exception as error:
        preparation_failed = True
        preparation_reason = _reason(error)
        preparation_diagnostic = _provider_diagnostic(error, calls.records)
    preparation_elapsed_ms = (perf_counter_ns() - started) // 1_000_000
    preparation_records = calls.records
    documents = dict(zip((doc.id for doc in manifest.documents), sources, strict=False))
    blocks = {block.id: block for source in sources for block in source.blocks}
    document_catalog = {source.document.id: key for key, source in documents.items()}
    version_catalog = {source.version.id: key for key, source in documents.items()}
    retrieval, generation = build_services(
        engine,
        plan,
        embedding=query_embedding,
        index=index,
        generator=generator,
        support=support,
    )
    observations: list[CaseObservation] = []
    failures: list[LiveCaseFailure] = []
    for case in manifest.cases:
        if local_output is not None:
            local_output.begin_case(case.id, case.question)
        started = perf_counter_ns()
        record_start = len(calls.records)
        selected_ids: tuple[str, ...] = ()
        observation: CaseObservation | None = None
        reason = preparation_reason
        stage: FailureStage = preparation_stage if preparation_failed else "retrieval"
        diagnostic = preparation_diagnostic if preparation_failed else None
        failed_rules: tuple[ValidationRule, ...] = ()
        if not preparation_failed:
            try:
                retrieved = retrieval.retrieve(
                    context,
                    ResearchRetrievalInput(
                        question=case.question,
                        document_ids=tuple(
                            documents[key].document.id for key in case.scope_document_ids
                        ),
                        generation_model=ResearchModel(plan.configuration.answer_model),
                        support_model=ResearchModel(plan.configuration.support_model),
                    ),
                )
                stage = "passage_mapping"
                candidates = {candidate.id: candidate for candidate in retrieved.candidates}
                selected = []
                for source in retrieved.sources:
                    candidate = candidates[source.retrieval_candidate_id]
                    if candidate.selection_rank is None:
                        raise ValueError("selected evaluation candidate has no selection rank")
                    selected.append(
                        SelectedRetrievalCandidate(
                            **source.model_dump(exclude={"retrieval_candidate_id"}),
                            origin=candidate.origin,
                            rank=candidate.rank,
                            raw_score=candidate.raw_score,
                            selection_rank=candidate.selection_rank,
                        )
                    )
                selected_ids = map_selected_passages(
                    manifest,
                    selected,
                    blocks_by_id=blocks,
                    catalog_document_ids=document_catalog,
                )
                stage = "answer_generation_and_validation"
                result = generation.generate(context, retrieved)
                stage = "persistence_readback"
                _verify_persistence(engine, sources[0], result)
                if local_output is not None:
                    local_output.validated_result(result)
                if result.answer.state is AnswerState.REJECTED:
                    reason = "rejected"
                    stage = "evidence_validation"
                    # Reasons can contain model-authored text. Retain only the rule enums.
                    failed_rules = tuple(
                        sorted({item.rule for item in result.validations if not item.passed})
                    )
                else:
                    stage = "citation_mapping"
                    spans = {span.id: span for span in result.evidence_spans}
                    citation_mappings = map_citation_passages(
                        manifest,
                        tuple(spans[item.evidence_span_id] for item in result.statement_evidence),
                        blocks_by_id=blocks,
                        catalog_document_version_ids=version_catalog,
                    )
                    matched = {passage_id for ids in citation_mappings for passage_id in ids}
                    cited_ids = tuple(item.id for item in manifest.passages if item.id in matched)
                    observation = CaseObservation(
                        case_id=case.id,
                        selected_passage_ids=selected_ids,
                        cited_passage_ids=cited_ids,
                        citation_count=len(result.statement_evidence),
                        citation_passage_ids=citation_mappings,
                        terminal_status="COMPLETED",
                        coverage_status=(
                            OBSERVED_COVERAGE[result.answer.coverage_status]
                            if result.answer.coverage_status is not None
                            else None
                        ),
                        exact_citations_valid=True if result.evidence_spans else None,
                        support_assessment=None,
                        failed_safely=None,
                        elapsed_ms=(perf_counter_ns() - started) // 1_000_000,
                        provider_usage=summarize_usage(calls.records[record_start:]),
                    )
            except Exception as error:
                reason = _reason(error)
                diagnostic = _provider_diagnostic(error, calls.records[record_start:])
        if observation is None:
            failures.append(
                LiveCaseFailure(
                    case_id=case.id,
                    reason=reason,
                    stage=stage,
                    provider_failure=diagnostic,
                    failed_validation_rules=failed_rules,
                )
            )
            observation = CaseObservation(
                case_id=case.id,
                selected_passage_ids=selected_ids,
                cited_passage_ids=(),
                citation_count=0,
                citation_passage_ids=(),
                terminal_status="FAILED",
                coverage_status=None,
                exact_citations_valid=None,
                support_assessment=None,
                failed_safely=reason != "verification",
                elapsed_ms=(perf_counter_ns() - started) // 1_000_000,
                provider_usage=summarize_usage(calls.records[record_start:]),
            )
        observations.append(observation)
        if local_output is not None:
            local_output.finish_case(
                status=observation.terminal_status,
                diagnostic=diagnostic,
                failed_rules=failed_rules,
            )
    observation_set = ObservationSet(
        schema_version=2,
        corpus_id=plan.corpus_id,
        corpus_sha256=plan.corpus_sha256,
        configuration=plan.configuration,
        run_mode="live_opt_in",
        cases=tuple(observations),
    )
    return LiveBaselineReport(
        admission_plan=plan,
        observations=observation_set,
        summary=score_observations(observation_set, manifest, plan.corpus_sha256),
        failures=tuple(failures),
        preparation_failed=preparation_failed,
        preparation_elapsed_ms=preparation_elapsed_ms,
        provider_calls=calls.records,
        preparation_usage=summarize_usage(preparation_records),
        total_usage=summarize_usage(calls.records),
        reserved_cost_micro_usd=calls.budget.reserved_cost_micro_usd,
    )
