"""Process-specific composition roots for application services and adapters."""

from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import Engine

from investment_research import __version__
from investment_research.infrastructure.database.metadata import get_model_metadata
from investment_research.infrastructure.database.session import (
    SessionFactory,
    create_database_engine,
    create_session_factory,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_answer_validation_unit_of_work_factory,
    create_capture_unit_of_work_factory,
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
    OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
    OPENAI_STATEMENT_SUPPORT_MODEL,
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
    OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS,
    OPENAI_ANSWER_INPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
    OPENAI_ANSWER_MODEL,
    OPENAI_ANSWER_OUTPUT_MICRO_USD_PER_MILLION_TOKENS,
    OPENAI_ANSWER_TIMEOUT_SECONDS,
    OpenAIAnswerGenerator,
    create_openai_answer_generator,
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
from investment_research.infrastructure.operations.opentelemetry import (
    OpenTelemetryOperationTracer,
    create_operation_tracer,
)
from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.queue.celery import (
    CeleryBrokerReadinessProbe,
    CeleryProcessingDispatcher,
    create_celery_app,
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
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.documents.ports import ObjectStorage
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.evidence.contracts import EvidenceValidationProfile
from investment_research.modules.evidence.ports import StatementSupportEvaluator
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
)
from investment_research.modules.research.contracts import AnswerGenerationProfile
from investment_research.modules.research.ports import AnswerGenerator
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    DenseRetrievalProfile,
    LexicalSearchProfile,
    M1SelectionProfile,
)
from investment_research.modules.search.indexing import (
    DocumentEmbeddingAccountingProfile,
    IndexActiveChunksService,
)
from investment_research.settings import (
    ApiSettings,
    DatabaseSettings,
    ObjectStorageSettings,
    WorkerSettings,
)

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


@dataclass(frozen=True, slots=True)
class _DatabaseResources:
    """Process-owned database engine and bound session factory."""

    engine: Engine
    session_factory: SessionFactory


@dataclass(frozen=True, slots=True)
class _ObjectStorageResources:
    """One object-storage adapter and its optional provider readiness probe."""

    adapter: ObjectStorage
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
        return _ObjectStorageResources(
            adapter=LocalObjectStorage(settings.object_storage_root),
            readiness_probe=None,
        )

    s3 = settings.require_s3_settings()
    client = create_s3_client(
        endpoint_url=s3.endpoint_url,
        region_name=s3.region_name,
        access_key_id=s3.access_key_id.get_secret_value(),
        secret_access_key=s3.secret_access_key.get_secret_value(),
    )
    return _ObjectStorageResources(
        adapter=S3ObjectStorage(client=client, bucket=s3.bucket),
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
    answer_generator_provider: OpenAIAnswerGenerator | None
    statement_support_provider: OpenAIStatementSupportEvaluator | None
    operation_tracer: OpenTelemetryOperationTracer
    provider_operation_executor: ExecuteProviderOperationService | None
    processing_dispatcher: ProcessingDispatcher
    capture_document_service: CaptureDocumentService
    open_original_service: OpenOriginalService
    get_processing_run_service: GetProcessingRunService
    retrieve_research_service: RetrieveResearchService
    generate_validated_answer_service: GenerateValidatedAnswerService
    answer_research_request_service: AnswerResearchRequestService
    resolve_evidence_span_service: ResolveEvidenceSpanService

    def close(self) -> None:
        """Release process-wide resources owned by the API container."""

        try:
            if self.statement_support_provider is not None:
                self.statement_support_provider.close()
        finally:
            try:
                if self.answer_generator_provider is not None:
                    self.answer_generator_provider.close()
            finally:
                try:
                    if self.dense_retriever is not None:
                        self.dense_retriever.close()
                finally:
                    try:
                        if self.query_embedding_provider is not None:
                            self.query_embedding_provider.close()
                    finally:
                        try:
                            self.queue_application.close()
                        finally:
                            try:
                                self.operation_tracer.close()
                            finally:
                                self.database_engine.dispose()


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
    operation_tracer: OpenTelemetryOperationTracer
    provider_operation_executor: ExecuteProviderOperationService | None
    open_original_service: OpenOriginalService
    process_document_service: ProcessDocumentService
    index_active_chunks_service: IndexActiveChunksService | None
    index_processing_run_service: IndexProcessingRunService | None
    execute_processing_job_service: ExecuteProcessingJobService

    def close(self) -> None:
        """Release process-wide resources owned by the worker container."""

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
    answer_generator_provider: OpenAIAnswerGenerator | None = None
    statement_support_provider: OpenAIStatementSupportEvaluator | None = None
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
    validation_profile = M0_0_EVIDENCE_VALIDATION_PROFILE
    lexical_candidate_limit = 10
    if settings.query_providers is not None:
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
            answer_generator_provider = create_openai_answer_generator(
                api_key=generation_settings.api_key.get_secret_value(),
                model=OPENAI_ANSWER_MODEL,
                operation_timeout_seconds=OPENAI_ANSWER_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_ANSWER_MAX_OUTPUT_TOKENS,
            )
            statement_support_provider = create_openai_statement_support_evaluator(
                api_key=generation_settings.api_key.get_secret_value(),
                model=OPENAI_STATEMENT_SUPPORT_MODEL,
                operation_timeout_seconds=OPENAI_STATEMENT_SUPPORT_TIMEOUT_SECONDS,
                max_output_tokens=OPENAI_STATEMENT_SUPPORT_MAX_OUTPUT_TOKENS,
            )
        except Exception:
            try:
                if statement_support_provider is not None:
                    statement_support_provider.close()
            finally:
                try:
                    if answer_generator_provider is not None:
                        answer_generator_provider.close()
                finally:
                    try:
                        if dense_retriever is not None:
                            dense_retriever.close()
                    finally:
                        try:
                            if query_embedding_provider is not None:
                                query_embedding_provider.close()
                        finally:
                            try:
                                queue_application.close()
                            finally:
                                try:
                                    operation_tracer.close()
                                finally:
                                    database.engine.dispose()
            raise
        lexical_candidate_limit = M1_LANE_CANDIDATE_LIMIT
        m1_query_retrieval = M1QueryRetrievalDependencies(
            embedding_provider=query_embedding_provider,
            dense_retriever=dense_retriever,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=provider_settings.embedding.config_version,
                dense_index_config_version=provider_settings.pinecone.config_version,
                candidate_limit=M1_LANE_CANDIDATE_LIMIT,
            ),
            selection_profile=M1SelectionProfile(),
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
        generation_profile = M1_GENERATION_PROFILE
        generation_accounting = AnswerGenerationAccountingProfile(
            provider="openai",
            model=OPENAI_ANSWER_MODEL,
            input_overhead_units=OPENAI_ANSWER_ACCOUNTING_INPUT_OVERHEAD_UNITS,
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
        validation_profile = M1_EVIDENCE_VALIDATION_PROFILE
    retrieve_research_service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(
            database.session_factory
        ),
        search_profile=LexicalSearchProfile(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            candidate_limit=lexical_candidate_limit,
        ),
        m1_query_retrieval=m1_query_retrieval,
    )
    generate_validated_answer_service = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(
            database.session_factory
        ),
        answer_generator=answer_generator,
        support_evaluator=support_evaluator,
        generation_profile=generation_profile,
        validation_profile=validation_profile,
        insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
        provider_operation_executor=provider_operation_executor,
        generation_accounting=generation_accounting,
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
        operation_tracer=operation_tracer,
        provider_operation_executor=provider_operation_executor,
        processing_dispatcher=processing_dispatcher,
        capture_document_service=CaptureDocumentService(
            object_storage=object_storage.adapter,
            unit_of_work_factory=create_capture_unit_of_work_factory(database.session_factory),
            max_upload_bytes=settings.max_upload_bytes,
            processing_dispatcher=processing_dispatcher,
            processing_profile=M1_PROCESSING_PROFILE,
        ),
        open_original_service=open_original_service,
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
            provider_settings = settings.require_indexing_providers()
            monthly_budget = settings.provider_monthly_budget_micro_usd
            assert monthly_budget is not None
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
            index_active_chunks_service = IndexActiveChunksService(
                embedding_provider=embedding_provider,
                dense_index_writer=dense_index_writer,
                provider_operation_executor=provider_operation_executor,
                embedding_accounting=DocumentEmbeddingAccountingProfile(
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
            index_processing_run_service = IndexProcessingRunService(
                indexing_service=index_active_chunks_service,
                unit_of_work_factory=create_processing_unit_of_work_factory(
                    database.session_factory
                ),
                profile=processing_profile,
            )
        return WorkerContainer(
            settings=settings,
            identity_context_provider=identity_context_provider,
            database_engine=database.engine,
            session_factory=database.session_factory,
            object_storage=object_storage.adapter,
            embedding_provider=embedding_provider,
            dense_index_writer=dense_index_writer,
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
        )
    except Exception:
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
