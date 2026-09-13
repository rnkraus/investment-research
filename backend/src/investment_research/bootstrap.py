"""Composition root for application-owned services and adapters."""

from dataclasses import dataclass

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
from investment_research.infrastructure.object_storage.local import LocalObjectStorage
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
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
    GetProcessingRunService,
    ProcessDocumentService,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.research.application import (
    AnswerResearchRequestService,
    GenerateValidatedAnswerService,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import AnswerGenerationProfile
from investment_research.modules.search.contracts import LexicalSearchProfile
from investment_research.settings import Settings

M0_0_GENERATION_PROFILE = AnswerGenerationProfile(
    generator_version="deterministic-fixture-v1",
    generation_config_version="m0.0-generation-v1",
    prompt_version="m0.0-prompt-v1",
)
M0_0_EVIDENCE_VALIDATION_PROFILE = EvidenceValidationProfile(
    validation_config_version="m0.0-evidence-v1"
)


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Explicit dependencies available to API and runner entry points."""

    settings: Settings
    identity_context_provider: IdentityContextProvider
    database_engine: Engine
    session_factory: SessionFactory
    object_storage: ObjectStorage
    capture_document_service: CaptureDocumentService
    open_original_service: OpenOriginalService
    get_processing_run_service: GetProcessingRunService
    process_document_service: ProcessDocumentService
    retrieve_research_service: RetrieveResearchService
    generate_validated_answer_service: GenerateValidatedAnswerService
    answer_research_request_service: AnswerResearchRequestService
    resolve_evidence_span_service: ResolveEvidenceSpanService

    def close(self) -> None:
        """Release process-wide resources owned by the container."""

        self.database_engine.dispose()


def build_container(settings: Settings) -> ApplicationContainer:
    """Wire the local M0.0 adapters behind application-owned contracts."""

    get_model_metadata()
    database_engine = create_database_engine(settings)
    session_factory = create_session_factory(database_engine)
    object_storage = LocalObjectStorage(settings.object_storage_root)
    retrieve_research_service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(session_factory),
        search_profile=LexicalSearchProfile(
            lexical_config_version=M0_0_PROCESSING_PROFILE.lexical_config_version,
            candidate_limit=10,
        ),
    )
    generate_validated_answer_service = GenerateValidatedAnswerService(
        unit_of_work_factory=create_answer_validation_unit_of_work_factory(session_factory),
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
        unit_of_work_factory=create_document_read_unit_of_work_factory(session_factory),
    )
    return ApplicationContainer(
        settings=settings,
        identity_context_provider=FixedLocalIdentityContextProvider(),
        database_engine=database_engine,
        session_factory=session_factory,
        object_storage=object_storage,
        capture_document_service=CaptureDocumentService(
            object_storage=object_storage,
            unit_of_work_factory=create_capture_unit_of_work_factory(session_factory),
            max_upload_bytes=settings.max_upload_bytes,
        ),
        open_original_service=open_original_service,
        get_processing_run_service=GetProcessingRunService(
            unit_of_work_factory=create_processing_read_unit_of_work_factory(session_factory),
        ),
        process_document_service=ProcessDocumentService(
            parser=PypdfDocumentParser(),
            original_provider=open_original_service,
            unit_of_work_factory=create_processing_unit_of_work_factory(session_factory),
            parser_limits=ParserLimits(
                max_pages=settings.max_pdf_pages,
                max_extracted_characters=settings.max_extracted_characters,
            ),
        ),
        retrieve_research_service=retrieve_research_service,
        generate_validated_answer_service=generate_validated_answer_service,
        answer_research_request_service=AnswerResearchRequestService(
            retrieval_service=retrieve_research_service,
            generation_service=generate_validated_answer_service,
        ),
        resolve_evidence_span_service=ResolveEvidenceSpanService(
            create_evidence_read_unit_of_work_factory(session_factory)
        ),
    )
