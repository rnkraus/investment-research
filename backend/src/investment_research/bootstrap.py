"""Process-specific composition roots for application services and adapters."""

import json
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import Engine

from investment_research import __version__
from investment_research.infrastructure.acceptance import (
    M1_ACCEPTANCE_GENERATION_PROFILE,
    M1_ACCEPTANCE_SUPPORT_CONFIG_VERSION,
    create_m1_acceptance_answer_generator,
    create_m1_acceptance_embedding_provider,
    create_m1_acceptance_support_evaluator,
)
from investment_research.infrastructure.database.document_deletion import (
    create_document_deletion_unit_of_work_factory,
)
from investment_research.infrastructure.database.metadata import get_model_metadata
from investment_research.infrastructure.database.outbox_unit_of_work import (
    create_outbox_delivery_unit_of_work_factory,
)
from investment_research.infrastructure.database.session import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
    create_capture_unit_of_work_factory,
    create_citable_source_unit_of_work_factory,
    create_document_read_unit_of_work_factory,
    create_evidence_read_unit_of_work_factory,
    create_processing_read_unit_of_work_factory,
    create_processing_unit_of_work_factory,
    create_provider_operation_unit_of_work_factory,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.infrastructure.evidence.openai import (
    M1_STATEMENT_SUPPORT_CONFIG_VERSION,
    OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS,
    OPENAI_STATEMENT_SUPPORT_INPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
    OPENAI_STATEMENT_SUPPORT_MODEL,
    OPENAI_STATEMENT_SUPPORT_OUTPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
    OpenAIStatementSupportEvaluator,
    create_openai_statement_support_evaluator,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
    DeterministicFixtureAnswerGenerator,
)
from investment_research.infrastructure.generation.openai import (
    M1_ANSWER_GENERATION_CONFIG_VERSION,
    M1_ANSWER_GENERATOR_VERSION,
    M1_ANSWER_PROMPT_VERSION,
    OPENAI_ANSWER_INPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_OUTPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
    create_openai_answer_generator,
)
from investment_research.infrastructure.generation.openai_citable_units import (
    OpenAICitableUnitAnswerGenerator,
    citable_answer_profile,
    create_openai_citable_unit_answer_generator,
)
from investment_research.infrastructure.health import (
    CompositeReadinessProbe,
    PostgreSQLReadinessProbe,
    ReadinessProbe,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.infrastructure.object_storage.s3 import (
    S3ObjectStorage,
    S3ObjectStorageReadinessProbe,
    create_s3_client,
)
from investment_research.infrastructure.openai_models import (
    RESEARCH_MODEL_PRICES,
    answer_config_version,
    support_config_version,
)
from investment_research.infrastructure.operations.opentelemetry import (
    OpenTelemetryOperationTracer,
    create_operation_tracer,
)
from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.queue.celery import (
    CeleryBrokerReadinessProbe,
    CeleryDocumentPurgeDispatcher,
    CeleryProcessingDispatcher,
    create_celery_app,
)
from investment_research.infrastructure.queue.outbox import BoundedCeleryOutboxPublisher
from investment_research.infrastructure.search.deterministic_index import (
    DeterministicSqliteDenseIndex,
)
from investment_research.infrastructure.search.openai import (
    OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS,
    OpenAIEmbeddingProvider,
    create_openai_embedding_provider,
)
from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    PineconeDenseRetriever,
    create_pinecone_dense_index_writer,
    create_pinecone_dense_retriever,
)
from investment_research.modules.delivery.repair import (
    ExecuteOutboxRepairService,
    RepairOutboxService,
)
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.documents.deletion import (
    ExecuteDocumentPurgeJobService,
    GetDocumentDeletionService,
    PurgeDocumentService,
    RequestDocumentDeletionService,
    SubmitDocumentDeletionService,
)
from investment_research.modules.documents.deletion_ports import OriginalPurger
from investment_research.modules.documents.listing import ListDocumentsService
from investment_research.modules.documents.ports import ObjectStorage
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.evidence.contracts import (
    EvidenceValidationDecision,
    EvidenceValidationProfile,
)
from investment_research.modules.evidence.ports import StatementSupportEvaluator
from investment_research.modules.evidence.support_context import (
    build_document_contextual_support_input,
)
from investment_research.modules.identity.application import (
    FixedLocalIdentityContextProvider,
    IdentityContextProvider,
)
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
    ExecuteProcessingJobService,
    GetProcessingRunService,
    ProcessDocumentService,
    ProcessingProfile,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.processing.indexing import IndexProcessingRunService
from investment_research.modules.processing.ports import DocumentParser, ProcessingDispatcher
from investment_research.modules.research.application import (
    AnswerGenerationAccountingProfile,
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    RetrieveResearchService,
    StatementSupportAccountingProfile,
)
from investment_research.modules.research.citable_generation import CitableGenerationInputBuilder
from investment_research.modules.research.contracts import (
    AnswerGenerationProfile,
    ResearchModel,
    UnvalidatedGeneratedAnswer,
)
from investment_research.modules.research.ports import AnswerGenerator
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    DenseRetrievalProfile,
    LexicalSearchProfile,
    M1SelectionProfile,
)
from investment_research.modules.search.fusion_contracts import RrfSelectionProfile
from investment_research.modules.search.indexing import (
    DocumentEmbeddingAccountingProfile,
    IndexActiveChunksService,
)
from investment_research.modules.search.ports import (
    DenseIndexWriter,
    EmbeddingProvider,
)
from investment_research.settings import (
    ApiSettings,
    DatabaseSettings,
    ObjectStorageSettings,
    QueueSettings,
    WorkerSettings,
)

type RuntimeAnswerGenerator = OpenAIAnswerGenerator | OpenAICitableUnitAnswerGenerator

M0_0_GENERATION_PROFILE = AnswerGenerationProfile(
    generator_version="deterministic-fixture-v1",
    generation_config_version="m0.0-generation-v1",
    prompt_version="m0.0-prompt-v1",
)
M0_0_EVIDENCE_VALIDATION_PROFILE = EvidenceValidationProfile(
    validation_config_version="m0.0-evidence-v1"
)
M1_GENERATION_PROFILE = AnswerGenerationProfile(
    generator_version=M1_ANSWER_GENERATOR_VERSION,
    generation_config_version=M1_ANSWER_GENERATION_CONFIG_VERSION,
    prompt_version=M1_ANSWER_PROMPT_VERSION,
)
M1_EVIDENCE_VALIDATION_PROFILE = EvidenceValidationProfile(
    validation_config_version="m1-evidence-validation-v1"
)
_MAX_LOCAL_MODEL_FAILURE_OUTPUT_CHARACTERS = 100_000


def _local_model_failure_sink(role: str, model: str) -> Callable[[str], None]:
    """Show opt-in local model text outside normal sanitized application logging."""

    def emit(output_text: str) -> None:
        maximum = _MAX_LOCAL_MODEL_FAILURE_OUTPUT_CHARACTERS
        payload = {
            "event": "local.model_failure_output",
            "role": role,
            "model": model,
            "output_text": output_text[:maximum],
            "truncated": len(output_text) > maximum,
        }
        try:
            sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stderr.flush()
        except OSError, ValueError:
            pass

    return emit


def _local_evidence_rejection_sink(
    answer_model: ResearchModel,
    support_model: ResearchModel,
) -> Callable[[UnvalidatedGeneratedAnswer, EvidenceValidationDecision], None]:
    """Show only failed proposal parts in the opt-in local model diagnostic."""

    emit = _local_model_failure_sink("evidence_validation", f"{answer_model}/{support_model}")

    def report(
        proposal: UnvalidatedGeneratedAnswer,
        decision: EvidenceValidationDecision,
    ) -> None:
        failed_findings = tuple(finding for finding in decision.findings if not finding.passed)
        rejected_ordinals = {
            finding.statement_ordinal
            for finding in failed_findings
            if finding.statement_ordinal is not None
        }
        payload = {
            "coverage_status": proposal.coverage_status,
            "rendered_text": proposal.rendered_text,
            "rejected_statements": [
                {
                    "ordinal": ordinal,
                    "text": statement.text,
                    "evidence": [
                        {
                            "retrieval_candidate_id": str(reference.retrieval_candidate_id),
                            "exact_text": reference.exact_text,
                        }
                        for reference in statement.evidence
                    ],
                }
                for ordinal, statement in enumerate(proposal.statements)
                if ordinal in rejected_ordinals
            ],
            "failed_findings": [
                {
                    "rule": finding.rule,
                    "statement_ordinal": finding.statement_ordinal,
                    "reason": finding.reason,
                }
                for finding in failed_findings
            ],
        }
        emit(json.dumps(payload, ensure_ascii=False))

    return report


@dataclass(frozen=True, slots=True)
class _DatabaseResources:
    """Process-owned database engine and bound session factory."""

    engine: Engine
    session_factory: SessionFactory


@dataclass(frozen=True, slots=True)
class _ObjectStorageResources:
    """One object-storage adapter and its optional provider readiness probe."""

    adapter: ObjectStorage
    purger: OriginalPurger
    readiness_probe: ReadinessProbe | None


def _build_database_resources(settings: DatabaseSettings) -> _DatabaseResources:
    """Create database resources shared by one process's application services."""

    get_model_metadata()
    engine = create_database_engine(settings)
    return _DatabaseResources(
        engine=engine,
        session_factory=create_session_factory(engine),
    )


def _build_object_storage(settings: ObjectStorageSettings) -> _ObjectStorageResources:
    """Select one adapter and provider probe at the process composition boundary."""

    if settings.object_storage_provider == "local":
        local_storage = LocalObjectStorage(settings.object_storage_root)
        return _ObjectStorageResources(
            adapter=local_storage,
            purger=local_storage,
            readiness_probe=None,
        )

    s3 = settings.require_s3_settings()
    client = create_s3_client(
        endpoint_url=s3.endpoint_url,
        region_name=s3.region_name,
        access_key_id=s3.access_key_id.get_secret_value(),
        secret_access_key=s3.secret_access_key.get_secret_value(),
    )
    storage = S3ObjectStorage(client=client, bucket=s3.bucket)
    return _ObjectStorageResources(
        adapter=storage,
        purger=storage,
        readiness_probe=S3ObjectStorageReadinessProbe(client=client, bucket=s3.bucket),
    )


@dataclass(frozen=True, slots=True)
class ApiContainer:
    """Dependencies owned by the FastAPI process."""

    settings: ApiSettings
    identity_context_provider: IdentityContextProvider
    database_engine: Engine
    session_factory: SessionFactory
    readiness_probe: ReadinessProbe
    object_storage: ObjectStorage
    queue_application: Celery
    query_embedding_provider: OpenAIEmbeddingProvider | None
    dense_retriever: PineconeDenseRetriever | None
    answer_generator_provider: RuntimeAnswerGenerator | None
    statement_support_provider: OpenAIStatementSupportEvaluator | None
    luna_answer_generator_provider: RuntimeAnswerGenerator | None
    luna_statement_support_provider: OpenAIStatementSupportEvaluator | None
    acceptance_dense_index: DeterministicSqliteDenseIndex | None
    operation_tracer: OpenTelemetryOperationTracer
    provider_operation_executor: ExecuteProviderOperationService | None
    processing_dispatcher: ProcessingDispatcher
    capture_document_service: CaptureDocumentService
    open_original_service: OpenOriginalService
    list_documents_service: ListDocumentsService
    submit_document_deletion_service: SubmitDocumentDeletionService
    get_document_deletion_service: GetDocumentDeletionService
    get_processing_run_service: GetProcessingRunService
    retrieve_research_service: RetrieveResearchService
    generate_validated_answer_service: GenerateValidatedAnswerService
    answer_research_request_service: AnswerResearchRequestService
    resolve_evidence_span_service: ResolveEvidenceSpanService

    def close(self) -> None:
        """Release process-wide resources owned by the API container."""

        with ExitStack() as stack:
            stack.callback(self.database_engine.dispose)
            stack.callback(self.operation_tracer.close)
            stack.callback(self.queue_application.close)
            for resource in (
                self.query_embedding_provider,
                self.dense_retriever,
                self.answer_generator_provider,
                self.statement_support_provider,
                self.luna_answer_generator_provider,
                self.luna_statement_support_provider,
                self.acceptance_dense_index,
            ):
                if resource is not None:
                    stack.callback(resource.close)


@dataclass(frozen=True, slots=True)
class WorkerContainer:
    """Process-owned dependencies used by a Celery worker child."""

    settings: WorkerSettings
    identity_context_provider: IdentityContextProvider
    database_engine: Engine
    session_factory: SessionFactory
    object_storage: ObjectStorage
    embedding_provider: OpenAIEmbeddingProvider | None
    dense_index_writer: PineconeDenseIndexWriter | None
    acceptance_dense_index: DeterministicSqliteDenseIndex | None
    operation_tracer: OpenTelemetryOperationTracer
    provider_operation_executor: ExecuteProviderOperationService | None
    open_original_service: OpenOriginalService
    process_document_service: ProcessDocumentService
    index_active_chunks_service: IndexActiveChunksService | None
    index_processing_run_service: IndexProcessingRunService | None
    execute_processing_job_service: ExecuteProcessingJobService
    purge_document_service: PurgeDocumentService
    execute_document_purge_job_service: ExecuteDocumentPurgeJobService
    execute_outbox_repair_service: ExecuteOutboxRepairService

    def close(self) -> None:
        """Release process-wide resources owned by the worker container."""

        try:
            if self.acceptance_dense_index is not None:
                self.acceptance_dense_index.close()
        finally:
            try:
                if self.dense_index_writer is not None:
                    self.dense_index_writer.close()
            finally:
                try:
                    if self.embedding_provider is not None:
                        self.embedding_provider.close()
                finally:
                    try:
                        self.operation_tracer.close()
                    finally:
                        self.database_engine.dispose()


def build_api_container(settings: ApiSettings) -> ApiContainer:
    """Wire API application services without constructing parser execution."""

    processing_profile: ProcessingProfile = (
        M0_0_PROCESSING_PROFILE
        if settings.processing_profile == "m0-regression"
        else M1_PROCESSING_PROFILE
    )
    queue_application = create_celery_app(settings)
    processing_dispatcher = CeleryProcessingDispatcher(queue_application)
    database = _build_database_resources(settings)
    object_storage = _build_object_storage(settings)
    operation_tracer = create_operation_tracer(
        service_name="investment-research-api",
        service_version=__version__,
    )
    readiness_probes: list[ReadinessProbe] = [
        PostgreSQLReadinessProbe(database.engine),
        CeleryBrokerReadinessProbe(queue_application),
    ]
    if object_storage.readiness_probe is not None:
        readiness_probes.append(object_storage.readiness_probe)
    query_embedding_provider: OpenAIEmbeddingProvider | None = None
    dense_retriever: PineconeDenseRetriever | None = None
    answer_generator_provider: RuntimeAnswerGenerator | None = None
    statement_support_provider: OpenAIStatementSupportEvaluator | None = None
    luna_answer_generator_provider: RuntimeAnswerGenerator | None = None
    luna_statement_support_provider: OpenAIStatementSupportEvaluator | None = None
    acceptance_dense_index: DeterministicSqliteDenseIndex | None = None
    provider_operation_executor: ExecuteProviderOperationService | None = None
    m1_query_retrieval: M1QueryRetrievalDependencies | None = None
    answer_generator: AnswerGenerator = DeterministicFixtureAnswerGenerator(
        answerable_case=M0_0_FIXTURE_ANSWER_CASE,
        insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
    )
    support_evaluator: StatementSupportEvaluator = DeterministicFixtureStatementSupportEvaluator(
        [
            FixtureStatementSupportRule(
                statement=M0_0_FIXTURE_ANSWER_CASE.statement,
                evidence_text=M0_0_FIXTURE_ANSWER_CASE.evidence_text,
            )
        ]
    )
    generation_profile = M0_0_GENERATION_PROFILE
    generation_accounting: AnswerGenerationAccountingProfile | None = None
    statement_support_accounting: StatementSupportAccountingProfile | None = None
    validation_profile = M0_0_EVIDENCE_VALIDATION_PROFILE
    lexical_candidate_limit = 10
    citable_units_enabled = settings.research_profile == "m1-5-citable-units"
    answer_factory = (
        create_openai_citable_unit_answer_generator
        if citable_units_enabled
        else create_openai_answer_generator
    )
    if settings.acceptance_dense_index_path is not None:
        monthly_budget = settings.provider_monthly_budget_micro_usd
        assert monthly_budget is not None
        provider_operation_unit_of_work_factory = create_provider_operation_unit_of_work_factory(
            database.session_factory
        )
        provider_operation_executor = ExecuteProviderOperationService(
            reservation_service=ReserveProviderOperationService(
                unit_of_work_factory=provider_operation_unit_of_work_factory,
                budget_policy=ProviderBudgetPolicy(
                    monthly_limit_micro_usd=monthly_budget,
                ),
            ),
            finalization_service=FinalizeProviderOperationService(
                unit_of_work_factory=provider_operation_unit_of_work_factory,
            ),
            tracer=operation_tracer,
        )
        try:
            acceptance_dense_index = DeterministicSqliteDenseIndex(
                settings.require_acceptance_dense_index_path()
            )
        except Exception:
            try:
                queue_application.close()
            finally:
                try:
                    operation_tracer.close()
                finally:
                    database.engine.dispose()
            raise
        acceptance_embedding_provider = create_m1_acceptance_embedding_provider()
        lexical_candidate_limit = M1_LANE_CANDIDATE_LIMIT
        m1_query_retrieval = M1QueryRetrievalDependencies(
            embedding_provider=acceptance_embedding_provider,
            dense_retriever=acceptance_dense_index,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                candidate_limit=M1_LANE_CANDIDATE_LIMIT,
            ),
            selection_profile=M1SelectionProfile(),
            provider_operation_executor=provider_operation_executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="deterministic",
                model="deterministic-m1-acceptance-embedding-v1",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=0,
                    output_micro_usd_per_million_units=0,
                ),
            ),
        )
        answer_generator = create_m1_acceptance_answer_generator()
        support_evaluator = create_m1_acceptance_support_evaluator()
        generation_profile = M1_ACCEPTANCE_GENERATION_PROFILE
        generation_accounting = AnswerGenerationAccountingProfile(
            provider="deterministic",
            model="deterministic-m1-acceptance-answer-v1",
            input_overhead_units=1,
            maximum_output_units=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
            cost_policy=ProviderCostPolicy(
                input_micro_usd_per_million_units=0,
                output_micro_usd_per_million_units=0,
            ),
        )
        statement_support_accounting = StatementSupportAccountingProfile(
            provider="deterministic",
            model="deterministic-m1-acceptance-support-v1",
            configuration_version=M1_ACCEPTANCE_SUPPORT_CONFIG_VERSION,
            input_overhead_units=1,
            maximum_output_units=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
            cost_policy=ProviderCostPolicy(
                input_micro_usd_per_million_units=0,
                output_micro_usd_per_million_units=0,
            ),
        )
        validation_profile = M1_EVIDENCE_VALIDATION_PROFILE
    elif settings.query_providers is not None:
        provider_settings = settings.require_query_providers()
        generation_settings = settings.require_generation_provider()
        monthly_budget = settings.provider_monthly_budget_micro_usd
        assert monthly_budget is not None
        provider_operation_unit_of_work_factory = create_provider_operation_unit_of_work_factory(
            database.session_factory
        )
        provider_operation_executor = ExecuteProviderOperationService(
            reservation_service=ReserveProviderOperationService(
                unit_of_work_factory=provider_operation_unit_of_work_factory,
                budget_policy=ProviderBudgetPolicy(
                    monthly_limit_micro_usd=monthly_budget,
                ),
            ),
            finalization_service=FinalizeProviderOperationService(
                unit_of_work_factory=provider_operation_unit_of_work_factory,
            ),
            tracer=operation_tracer,
        )
        try:
            query_embedding_provider = create_openai_embedding_provider(
                api_key=provider_settings.embedding.api_key.get_secret_value(),
                embedding_config_version=provider_settings.embedding.config_version,
                model=provider_settings.embedding.model,
                dimensions=provider_settings.embedding.dimensions,
                operation_timeout_seconds=provider_settings.embedding.timeout_seconds,
            )
            dense_retriever = create_pinecone_dense_retriever(
                api_key=provider_settings.pinecone.api_key.get_secret_value(),
                index_host=provider_settings.pinecone.index_host,
                operation_timeout_seconds=(provider_settings.pinecone.operation_timeout_seconds),
            )
            answer_generator_provider = answer_factory(
                api_key=generation_settings.api_key.get_secret_value(),
                model=OPENAI_ANSWER_MODEL,
                operation_timeout_seconds=OPENAI_ANSWER_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
                input_overhead_limit=settings.rag.generation.input_overhead_limit,
                local_validation_output=(
                    _local_model_failure_sink("answer", OPENAI_ANSWER_MODEL)
                    if settings.local_model_failure_output
                    else None
                ),
            )
            statement_support_provider = create_openai_statement_support_evaluator(
                api_key=generation_settings.api_key.get_secret_value(),
                model=OPENAI_STATEMENT_SUPPORT_MODEL,
                operation_timeout_seconds=OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
                local_validation_output=(
                    _local_model_failure_sink("statement_support", OPENAI_STATEMENT_SUPPORT_MODEL)
                    if settings.local_model_failure_output
                    else None
                ),
                include_attribution_context=citable_units_enabled,
                include_document_attribution_context=citable_units_enabled,
            )
            luna_answer_generator_provider = answer_factory(
                api_key=generation_settings.api_key.get_secret_value(),
                model=ResearchModel.LUNA,
                operation_timeout_seconds=OPENAI_ANSWER_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
                input_overhead_limit=settings.rag.generation.input_overhead_limit,
                local_validation_output=(
                    _local_model_failure_sink("answer", ResearchModel.LUNA)
                    if settings.local_model_failure_output
                    else None
                ),
            )
            luna_statement_support_provider = create_openai_statement_support_evaluator(
                api_key=generation_settings.api_key.get_secret_value(),
                model=ResearchModel.LUNA,
                operation_timeout_seconds=OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
                local_validation_output=(
                    _local_model_failure_sink("statement_support", ResearchModel.LUNA)
                    if settings.local_model_failure_output
                    else None
                ),
                include_attribution_context=citable_units_enabled,
                include_document_attribution_context=citable_units_enabled,
            )
        except Exception:
            with ExitStack() as stack:
                stack.callback(database.engine.dispose)
                stack.callback(operation_tracer.close)
                stack.callback(queue_application.close)
                for resource in (
                    query_embedding_provider,
                    dense_retriever,
                    answer_generator_provider,
                    statement_support_provider,
                    luna_answer_generator_provider,
                    luna_statement_support_provider,
                ):
                    if resource is not None:
                        stack.callback(resource.close)
            raise
        lexical_candidate_limit = settings.rag.retrieval.lexical_candidate_limit
        m1_query_retrieval = M1QueryRetrievalDependencies(
            embedding_provider=query_embedding_provider,
            dense_retriever=dense_retriever,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=provider_settings.embedding.config_version,
                dense_index_config_version=provider_settings.pinecone.config_version,
                candidate_limit=settings.rag.retrieval.dense_candidate_limit,
            ),
            selection_profile=(
                RrfSelectionProfile(
                    context_chunk_limit=settings.rag.context.chunk_limit,
                    context_character_limit=settings.rag.context.character_limit,
                )
                if citable_units_enabled
                else M1SelectionProfile(
                    context_chunk_limit=settings.rag.context.chunk_limit,
                    context_character_limit=settings.rag.context.character_limit,
                )
            ),
            provider_operation_executor=provider_operation_executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="openai",
                model=provider_settings.embedding.model,
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=(
                        OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS
                    ),
                    output_micro_usd_per_million_units=0,
                ),
            ),
        )
        answer_generator = answer_generator_provider
        support_evaluator = statement_support_provider
        generation_profile = (
            citable_answer_profile(ResearchModel.TERRA)
            if citable_units_enabled
            else M1_GENERATION_PROFILE
        )
        generation_accounting = AnswerGenerationAccountingProfile(
            provider="openai",
            model=OPENAI_ANSWER_MODEL,
            input_overhead_units=settings.rag.generation.input_overhead_limit,
            maximum_output_units=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
            cost_policy=ProviderCostPolicy(
                input_micro_usd_per_million_units=(
                    OPENAI_ANSWER_INPUT_MICRO_USD_PER_MILLION_TOKENS
                ),
                output_micro_usd_per_million_units=(
                    OPENAI_ANSWER_OUTPUT_MICRO_USD_PER_MILLION_TOKENS
                ),
            ),
        )
        statement_support_accounting = StatementSupportAccountingProfile(
            provider="openai",
            model=OPENAI_STATEMENT_SUPPORT_MODEL,
            configuration_version=(
                f"{M1_STATEMENT_SUPPORT_CONFIG_VERSION}-document-attribution-v1"
                if citable_units_enabled
                else M1_STATEMENT_SUPPORT_CONFIG_VERSION
            ),
            input_overhead_units=OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS,
            maximum_output_units=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
            cost_policy=ProviderCostPolicy(
                input_micro_usd_per_million_units=(
                    OPENAI_STATEMENT_SUPPORT_INPUT_MICRO_USD_PER_MILLION_TOKENS
                ),
                output_micro_usd_per_million_units=(
                    OPENAI_STATEMENT_SUPPORT_OUTPUT_MICRO_USD_PER_MILLION_TOKENS
                ),
            ),
        )
        validation_profile = (
            EvidenceValidationProfile(
                validation_config_version=(
                    f"m1-5-document-attribution-validation-v1-{ResearchModel.TERRA.value}"
                )
            )
            if citable_units_enabled
            else M1_EVIDENCE_VALIDATION_PROFILE
        )
    retrieve_research_service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(
            database.session_factory
        ),
        search_profile=LexicalSearchProfile(
            lexical_config_version=processing_profile.lexical_config_version,
            candidate_limit=lexical_candidate_limit,
        ),
        m1_query_retrieval=m1_query_retrieval,
    )
    answer_unit_of_work_factory = create_answer_validation_unit_of_work_factory(
        database.session_factory,
        include_document_attribution=citable_units_enabled,
    )
    generation_input_builder = (
        CitableGenerationInputBuilder(
            create_citable_source_unit_of_work_factory(database.session_factory)
        )
        if citable_units_enabled
        else None
    )
    support_context_builder = (
        build_document_contextual_support_input if citable_units_enabled else None
    )
    generate_validated_answer_service = GenerateValidatedAnswerService(
        unit_of_work_factory=answer_unit_of_work_factory,
        generation_input_builder=generation_input_builder,
        support_context_builder=support_context_builder,
        answer_generator=answer_generator,
        support_evaluator=support_evaluator,
        generation_profile=generation_profile,
        validation_profile=validation_profile,
        insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
        provider_operation_executor=provider_operation_executor,
        generation_accounting=generation_accounting,
        statement_support_accounting=statement_support_accounting,
        rejection_diagnostic=(
            _local_evidence_rejection_sink(ResearchModel.TERRA, ResearchModel.TERRA)
            if settings.local_model_failure_output and settings.query_providers is not None
            else None
        ),
    )
    model_services: (
        dict[tuple[ResearchModel, ResearchModel], GenerateValidatedAnswerService] | None
    ) = None
    if settings.query_providers is not None:
        assert answer_generator_provider is not None
        assert statement_support_provider is not None
        assert luna_answer_generator_provider is not None
        assert luna_statement_support_provider is not None
        assert provider_operation_executor is not None
        generators = {
            ResearchModel.TERRA: answer_generator_provider,
            ResearchModel.LUNA: luna_answer_generator_provider,
        }
        evaluators = {
            ResearchModel.TERRA: statement_support_provider,
            ResearchModel.LUNA: luna_statement_support_provider,
        }
        model_services = {
            (ResearchModel.TERRA, ResearchModel.TERRA): generate_validated_answer_service
        }
        for answer_model in ResearchModel:
            for support_model in ResearchModel:
                if (answer_model, support_model) == (ResearchModel.TERRA, ResearchModel.TERRA):
                    continue
                answer_price = RESEARCH_MODEL_PRICES[answer_model]
                support_price = RESEARCH_MODEL_PRICES[support_model]
                model_services[(answer_model, support_model)] = GenerateValidatedAnswerService(
                    unit_of_work_factory=answer_unit_of_work_factory,
                    generation_input_builder=generation_input_builder,
                    support_context_builder=support_context_builder,
                    answer_generator=generators[answer_model],
                    support_evaluator=evaluators[support_model],
                    generation_profile=citable_answer_profile(answer_model)
                    if citable_units_enabled
                    else AnswerGenerationProfile(
                        generator_version=M1_ANSWER_GENERATOR_VERSION,
                        generation_config_version=answer_config_version(answer_model),
                        prompt_version=M1_ANSWER_PROMPT_VERSION,
                    ),
                    validation_profile=EvidenceValidationProfile(
                        validation_config_version=(
                            f"m1-5-document-attribution-validation-v1-{support_model.value}"
                            if citable_units_enabled
                            else M1_EVIDENCE_VALIDATION_PROFILE.validation_config_version
                            if support_model is ResearchModel.TERRA
                            else "m1-evidence-validation-v1-luna-support"
                        )
                    ),
                    insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
                    provider_operation_executor=provider_operation_executor,
                    generation_accounting=AnswerGenerationAccountingProfile(
                        provider="openai",
                        model=answer_model,
                        input_overhead_units=settings.rag.generation.input_overhead_limit,
                        maximum_output_units=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
                        cost_policy=ProviderCostPolicy(
                            input_micro_usd_per_million_units=(
                                answer_price.input_micro_usd_per_million_tokens
                            ),
                            output_micro_usd_per_million_units=(
                                answer_price.output_micro_usd_per_million_tokens
                            ),
                        ),
                    ),
                    statement_support_accounting=StatementSupportAccountingProfile(
                        provider="openai",
                        model=support_model,
                        configuration_version=(
                            f"{support_config_version(support_model)}-document-attribution-v1"
                            if citable_units_enabled
                            else support_config_version(support_model)
                        ),
                        input_overhead_units=(
                            OPENAI_STATEMENT_SUPPORT_ACCOUNTING_INPUT_OVERHEAD_UNITS
                        ),
                        maximum_output_units=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
                        cost_policy=ProviderCostPolicy(
                            input_micro_usd_per_million_units=(
                                support_price.input_micro_usd_per_million_tokens
                            ),
                            output_micro_usd_per_million_units=(
                                support_price.output_micro_usd_per_million_tokens
                            ),
                        ),
                    ),
                    rejection_diagnostic=(
                        _local_evidence_rejection_sink(answer_model, support_model)
                        if settings.local_model_failure_output
                        else None
                    ),
                )
    open_original_service = OpenOriginalService(
        object_storage=object_storage.adapter,
        unit_of_work_factory=create_document_read_unit_of_work_factory(database.session_factory),
    )
    return ApiContainer(
        settings=settings,
        identity_context_provider=FixedLocalIdentityContextProvider(),
        database_engine=database.engine,
        session_factory=database.session_factory,
        readiness_probe=CompositeReadinessProbe(tuple(readiness_probes)),
        object_storage=object_storage.adapter,
        queue_application=queue_application,
        query_embedding_provider=query_embedding_provider,
        dense_retriever=dense_retriever,
        answer_generator_provider=answer_generator_provider,
        statement_support_provider=statement_support_provider,
        luna_answer_generator_provider=luna_answer_generator_provider,
        luna_statement_support_provider=luna_statement_support_provider,
        acceptance_dense_index=acceptance_dense_index,
        operation_tracer=operation_tracer,
        provider_operation_executor=provider_operation_executor,
        processing_dispatcher=processing_dispatcher,
        capture_document_service=CaptureDocumentService(
            object_storage=object_storage.adapter,
            unit_of_work_factory=create_capture_unit_of_work_factory(database.session_factory),
            max_upload_bytes=settings.max_upload_bytes,
            processing_dispatcher=processing_dispatcher,
            processing_profile=processing_profile,
        ),
        open_original_service=open_original_service,
        list_documents_service=ListDocumentsService(
            unit_of_work_factory=create_document_read_unit_of_work_factory(
                database.session_factory
            ),
        ),
        submit_document_deletion_service=SubmitDocumentDeletionService(
            request_service=RequestDocumentDeletionService(
                create_document_deletion_unit_of_work_factory(database.session_factory)
            ),
            dispatcher=CeleryDocumentPurgeDispatcher(queue_application),
        ),
        get_document_deletion_service=GetDocumentDeletionService(
            create_document_deletion_unit_of_work_factory(database.session_factory)
        ),
        get_processing_run_service=GetProcessingRunService(
            unit_of_work_factory=create_processing_read_unit_of_work_factory(
                database.session_factory
            ),
        ),
        retrieve_research_service=retrieve_research_service,
        generate_validated_answer_service=generate_validated_answer_service,
        answer_research_request_service=AnswerResearchRequestService(
            retrieval_service=retrieve_research_service,
            generation_service=generate_validated_answer_service,
            model_services=model_services,
        ),
        resolve_evidence_span_service=ResolveEvidenceSpanService(
            create_evidence_read_unit_of_work_factory(database.session_factory)
        ),
    )


def build_worker_container(
    settings: WorkerSettings,
    *,
    parser: DocumentParser | None = None,
) -> WorkerContainer:
    """Wire document processing without constructing API-only services."""

    processing_profile: ProcessingProfile = (
        M0_0_PROCESSING_PROFILE
        if settings.processing_profile == "m0-regression"
        else M1_PROCESSING_PROFILE
    )
    database = _build_database_resources(settings)
    operation_tracer = create_operation_tracer(
        service_name="investment-research-worker",
        service_version=__version__,
    )
    embedding_provider: OpenAIEmbeddingProvider | None = None
    dense_index_writer: PineconeDenseIndexWriter | None = None
    acceptance_dense_index: DeterministicSqliteDenseIndex | None = None
    provider_operation_executor: ExecuteProviderOperationService | None = None
    try:
        object_storage = _build_object_storage(settings)
        open_original_service = OpenOriginalService(
            object_storage=object_storage.adapter,
            unit_of_work_factory=create_document_read_unit_of_work_factory(
                database.session_factory
            ),
        )
        identity_context_provider = FixedLocalIdentityContextProvider()
        active_parser = parser or HttpDocumentParser(
            endpoint_url=settings.require_parser_endpoint_url(),
            parser_version=processing_profile.parser_version,
            parser_config_version=processing_profile.parser_config_version,
            timeout_seconds=settings.processing_timeout_seconds,
            max_request_bytes=settings.max_parser_request_bytes,
        )
        process_document_service = ProcessDocumentService(
            parser=active_parser,
            original_provider=open_original_service,
            unit_of_work_factory=create_processing_unit_of_work_factory(database.session_factory),
            parser_limits=ParserLimits(
                max_pages=settings.max_pdf_pages,
                max_extracted_characters=settings.max_extracted_characters,
            ),
            profile=processing_profile,
        )
        index_active_chunks_service: IndexActiveChunksService | None = None
        index_processing_run_service: IndexProcessingRunService | None = None
        if settings.processing_profile == "m1":
            monthly_budget = settings.require_m1_processing_configuration()
            provider_operation_unit_of_work_factory = (
                create_provider_operation_unit_of_work_factory(database.session_factory)
            )
            provider_operation_executor = ExecuteProviderOperationService(
                reservation_service=ReserveProviderOperationService(
                    unit_of_work_factory=provider_operation_unit_of_work_factory,
                    budget_policy=ProviderBudgetPolicy(
                        monthly_limit_micro_usd=monthly_budget,
                    ),
                ),
                finalization_service=FinalizeProviderOperationService(
                    unit_of_work_factory=provider_operation_unit_of_work_factory,
                ),
                tracer=operation_tracer,
            )
            active_embedding_provider: EmbeddingProvider
            active_dense_index_writer: DenseIndexWriter
            embedding_accounting: DocumentEmbeddingAccountingProfile
            if settings.acceptance_dense_index_path is not None:
                acceptance_dense_index = DeterministicSqliteDenseIndex(
                    settings.require_acceptance_dense_index_path()
                )
                active_embedding_provider = create_m1_acceptance_embedding_provider()
                active_dense_index_writer = acceptance_dense_index
                embedding_accounting = DocumentEmbeddingAccountingProfile(
                    provider="deterministic",
                    model="deterministic-m1-acceptance-embedding-v1",
                    cost_policy=ProviderCostPolicy(
                        input_micro_usd_per_million_units=0,
                        output_micro_usd_per_million_units=0,
                    ),
                )
            else:
                provider_settings = settings.require_indexing_providers()
                embedding_provider = create_openai_embedding_provider(
                    api_key=provider_settings.embedding.api_key.get_secret_value(),
                    embedding_config_version=provider_settings.embedding.config_version,
                    model=provider_settings.embedding.model,
                    dimensions=provider_settings.embedding.dimensions,
                    operation_timeout_seconds=provider_settings.embedding.timeout_seconds,
                )
                dense_index_writer = create_pinecone_dense_index_writer(
                    api_key=provider_settings.pinecone.api_key.get_secret_value(),
                    index_host=provider_settings.pinecone.index_host,
                    operation_timeout_seconds=provider_settings.pinecone.operation_timeout_seconds,
                )
                active_embedding_provider = embedding_provider
                active_dense_index_writer = dense_index_writer
                embedding_accounting = DocumentEmbeddingAccountingProfile(
                    provider="openai",
                    model=provider_settings.embedding.model,
                    cost_policy=ProviderCostPolicy(
                        input_micro_usd_per_million_units=(
                            OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS
                        ),
                        output_micro_usd_per_million_units=0,
                    ),
                )
            index_active_chunks_service = IndexActiveChunksService(
                embedding_provider=active_embedding_provider,
                dense_index_writer=active_dense_index_writer,
                provider_operation_executor=provider_operation_executor,
                embedding_accounting=embedding_accounting,
            )
            index_processing_run_service = IndexProcessingRunService(
                indexing_service=index_active_chunks_service,
                unit_of_work_factory=create_processing_unit_of_work_factory(
                    database.session_factory
                ),
                profile=processing_profile,
            )
        purge_document_service = PurgeDocumentService(
            unit_of_work_factory=create_document_deletion_unit_of_work_factory(
                database.session_factory
            ),
            originals=object_storage.purger,
            vectors=acceptance_dense_index
            if acceptance_dense_index is not None
            else dense_index_writer,
        )
        return WorkerContainer(
            settings=settings,
            identity_context_provider=identity_context_provider,
            database_engine=database.engine,
            session_factory=database.session_factory,
            object_storage=object_storage.adapter,
            embedding_provider=embedding_provider,
            dense_index_writer=dense_index_writer,
            acceptance_dense_index=acceptance_dense_index,
            operation_tracer=operation_tracer,
            provider_operation_executor=provider_operation_executor,
            open_original_service=open_original_service,
            process_document_service=process_document_service,
            index_active_chunks_service=index_active_chunks_service,
            index_processing_run_service=index_processing_run_service,
            execute_processing_job_service=ExecuteProcessingJobService(
                processing_service=process_document_service,
                indexing_service=index_processing_run_service,
                identity_context_provider=identity_context_provider,
            ),
            purge_document_service=purge_document_service,
            execute_document_purge_job_service=ExecuteDocumentPurgeJobService(
                purge_service=purge_document_service,
                identity_context_provider=identity_context_provider,
            ),
            execute_outbox_repair_service=ExecuteOutboxRepairService(
                repair_service=RepairOutboxService(
                    unit_of_work_factory=create_outbox_delivery_unit_of_work_factory(
                        database.session_factory,
                        maximum_processing_attempts=processing_profile.maximum_attempts,
                    ),
                    publisher=BoundedCeleryOutboxPublisher(
                        QueueSettings(
                            runtime_location=settings.runtime_location,
                            redis_url=settings.redis_url,
                        )
                    ),
                    maximum_attempts=processing_profile.maximum_attempts,
                ),
                identity_context_provider=identity_context_provider,
            ),
        )
    except Exception:
        try:
            if acceptance_dense_index is not None:
                acceptance_dense_index.close()
        finally:
            try:
                if dense_index_writer is not None:
                    dense_index_writer.close()
            finally:
                try:
                    if embedding_provider is not None:
                        embedding_provider.close()
                finally:
                    try:
                        operation_tracer.close()
                    finally:
                        database.engine.dispose()
        raise
