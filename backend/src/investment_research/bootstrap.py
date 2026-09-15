"""Process-specific composition roots for application services and adapters."""

from dataclasses import dataclass

from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy import Engine

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
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.evidence.deterministic import (
    DeterministicFixtureStatementSupportEvaluator,
    FixtureStatementSupportRule,
)
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
    M0_0_INSUFFICIENT_MESSAGE,
    DeterministicFixtureAnswerGenerator,
)
from investment_research.infrastructure.health import (
    CompositeReadinessProbe,
    PostgreSQLReadinessProbe,
    ReadinessProbe,
)
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.infrastructure.object_storage.s3 import (
    S3ObjectStorage,
    create_s3_client,
)
from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.queue.celery import (
    CeleryProcessingDispatcher,
    create_celery_app,
)
from investment_research.modules.documents.application import (
    CaptureDocumentService,
    OpenOriginalService,
)
from investment_research.modules.documents.ports import ObjectStorage
from investment_research.modules.evidence.application import ResolveEvidenceSpanService
from investment_research.modules.evidence.contracts import EvidenceValidationProfile
from investment_research.modules.identity.application import (
    FixedLocalIdentityContextProvider,
    IdentityContextProvider,
)
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    ExecuteProcessingJobService,
    GetProcessingRunService,
    ProcessDocumentService,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.processing.ports import DocumentParser, ProcessingDispatcher
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import AnswerGenerationProfile
from investment_research.modules.search.contracts import LexicalSearchProfile
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


@dataclass(frozen=True, slots=True)
class _DatabaseResources:
    """Process-owned database engine and bound session factory."""

    engine: Engine
    session_factory: SessionFactory


def _build_database_resources(settings: DatabaseSettings) -> _DatabaseResources:
    """Create database resources shared by one process's application services."""

    get_model_metadata()
    engine = create_database_engine(settings)
    return _DatabaseResources(
        engine=engine,
        session_factory=create_session_factory(engine),
    )


def _build_object_storage(settings: ObjectStorageSettings) -> ObjectStorage:
    """Select one concrete adapter at the process composition boundary."""

    if settings.object_storage_provider == "local":
        return LocalObjectStorage(settings.object_storage_root)

    s3 = settings.require_s3_settings()
    client = create_s3_client(
        endpoint_url=s3.endpoint_url,
        region_name=s3.region_name,
        access_key_id=s3.access_key_id.get_secret_value(),
        secret_access_key=s3.secret_access_key.get_secret_value(),
    )
    return S3ObjectStorage(client=client, bucket=s3.bucket)


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

        self.queue_application.close()
        self.database_engine.dispose()


@dataclass(frozen=True, slots=True)
class WorkerContainer:
    """Dependencies owned by the M0.0 runner and later Celery worker."""

    settings: WorkerSettings
    identity_context_provider: IdentityContextProvider
    database_engine: Engine
    session_factory: SessionFactory
    object_storage: ObjectStorage
    open_original_service: OpenOriginalService
    process_document_service: ProcessDocumentService
    execute_processing_job_service: ExecuteProcessingJobService

    def close(self) -> None:
        """Release process-wide resources owned by the worker container."""

        self.database_engine.dispose()


def build_api_container(settings: ApiSettings) -> ApiContainer:
    """Wire API application services without constructing parser execution."""

    queue_application = create_celery_app(settings)
    processing_dispatcher = CeleryProcessingDispatcher(queue_application)
    database = _build_database_resources(settings)
    object_storage = _build_object_storage(settings)
    retrieve_research_service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(
            database.session_factory
        ),
        search_profile=LexicalSearchProfile(
            lexical_config_version=M0_0_PROCESSING_PROFILE.lexical_config_version,
            candidate_limit=10,
        ),
    )
    generate_validated_answer_service = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(
            database.session_factory
        ),
        answer_generator=DeterministicFixtureAnswerGenerator(
            answerable_case=M0_0_FIXTURE_ANSWER_CASE,
            insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
        ),
        support_evaluator=DeterministicFixtureStatementSupportEvaluator(
            [
                FixtureStatementSupportRule(
                    statement=M0_0_FIXTURE_ANSWER_CASE.statement,
                    evidence_text=M0_0_FIXTURE_ANSWER_CASE.evidence_text,
                )
            ]
        ),
        generation_profile=M0_0_GENERATION_PROFILE,
        validation_profile=M0_0_EVIDENCE_VALIDATION_PROFILE,
        insufficient_message=M0_0_INSUFFICIENT_MESSAGE,
    )
    open_original_service = OpenOriginalService(
        object_storage=object_storage,
        unit_of_work_factory=create_document_read_unit_of_work_factory(database.session_factory),
    )
    return ApiContainer(
        settings=settings,
        identity_context_provider=FixedLocalIdentityContextProvider(),
        database_engine=database.engine,
        session_factory=database.session_factory,
        readiness_probe=CompositeReadinessProbe((PostgreSQLReadinessProbe(database.engine),)),
        object_storage=object_storage,
        queue_application=queue_application,
        processing_dispatcher=processing_dispatcher,
        capture_document_service=CaptureDocumentService(
            object_storage=object_storage,
            unit_of_work_factory=create_capture_unit_of_work_factory(database.session_factory),
            max_upload_bytes=settings.max_upload_bytes,
            processing_dispatcher=processing_dispatcher,
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

    database = _build_database_resources(settings)
    object_storage = _build_object_storage(settings)
    open_original_service = OpenOriginalService(
        object_storage=object_storage,
        unit_of_work_factory=create_document_read_unit_of_work_factory(database.session_factory),
    )
    identity_context_provider = FixedLocalIdentityContextProvider()
    active_parser = parser or HttpDocumentParser(
        endpoint_url=settings.require_parser_endpoint_url(),
        parser_version=M0_0_PROCESSING_PROFILE.parser_version,
        parser_config_version=M0_0_PROCESSING_PROFILE.parser_config_version,
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
    )
    return WorkerContainer(
        settings=settings,
        identity_context_provider=identity_context_provider,
        database_engine=database.engine,
        session_factory=database.session_factory,
        object_storage=object_storage,
        open_original_service=open_original_service,
        process_document_service=process_document_service,
        execute_processing_job_service=ExecuteProcessingJobService(
            processing_service=process_document_service,
            identity_context_provider=identity_context_provider,
        ),
    )
