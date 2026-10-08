"""Versioned HTTP and stream contracts for the M0.0 API surface."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from investment_research.modules.documents.contracts import MAX_PASTED_TEXT_CHARACTERS
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import ResearchModel, ResearchProgressStage
from investment_research.modules.research.domain import TemporalMode

TrimmedTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
LanguageCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"),
]
Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
SafeMessage = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class StrictContract(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ErrorCode(StrEnum):
    """Stable public error categories without internal details."""

    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    EVIDENCE_VALIDATION_FAILED = "EVIDENCE_VALIDATION_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RESEARCH_CONTEXT_LIMIT = "RESEARCH_CONTEXT_LIMIT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(StrictContract):
    """Safe machine-readable error detail."""

    code: ErrorCode
    message: SafeMessage
    request_id: UUID | None = None


class ErrorResponse(StrictContract):
    """Envelope used by non-streaming API failures."""

    error: ErrorDetail


class DocumentMetadataInput(StrictContract):
    """Validated metadata fields submitted alongside an uploaded document."""

    title: TrimmedTitle
    language: LanguageCode = "en"
    published_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None


class PastedTextDocumentInput(DocumentMetadataInput):
    """Bounded JSON body for one immutable English pasted-text original."""

    language: Literal["en"] = "en"
    text: Annotated[str, StringConstraints(max_length=MAX_PASTED_TEXT_CHARACTERS)]


class DocumentAccepted(StrictContract):
    """Identifiers returned after immutable capture has been accepted."""

    document_id: UUID
    document_version_id: UUID
    processing_run_id: UUID
    state: Literal["CAPTURED"] = "CAPTURED"
    processing_status_url: str


class ProcessingRunResult(StrictContract):
    """Safe processing status visible to the owning workspace."""

    processing_run_id: UUID
    document_version_id: UUID
    state: ProcessingState
    error_summary: SafeMessage | None = None


class ResearchRequestContext(StrictContract):
    """Explicit archive filters supplied with a research question."""

    document_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    temporal_mode: TemporalMode = TemporalMode.CURRENT
    cutoff_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_temporal_cutoff(self) -> Self:
        """Reject ambiguous combinations before they reach application services."""

        if self.temporal_mode is TemporalMode.CURRENT and self.cutoff_at is not None:
            raise ValueError("CURRENT temporal mode must not define cutoff_at")
        if self.temporal_mode is not TemporalMode.CURRENT and self.cutoff_at is None:
            raise ValueError("historical temporal mode requires cutoff_at")
        return self


class ResearchRequestInput(StrictContract):
    """Question and explicit context accepted by the research endpoint."""

    question: Question
    context: ResearchRequestContext = Field(default_factory=ResearchRequestContext)
    generation_model: ResearchModel = ResearchModel.TERRA
    support_model: ResearchModel = ResearchModel.TERRA


class ResearchModelOptions(StrictContract):
    """Models genuinely available in the active API runtime."""

    provider_enabled: bool
    generation_models: tuple[ResearchModel, ...]
    support_models: tuple[ResearchModel, ...]


class EvidenceRelationship(StrEnum):
    """How a source span relates to an answer statement."""

    SUPPORTS = "SUPPORTS"
    CONFLICTS = "CONFLICTS"


class EvidencePdfPageLocator(StrictContract):
    """Physical one-based page inside an immutable PDF original."""

    kind: Literal["PDF_PAGE"] = "PDF_PAGE"
    page: int = Field(ge=1)


class EvidenceTextPassageLocator(StrictContract):
    """Absolute Python-code-point range inside an immutable plain-text original."""

    kind: Literal["TEXT_PASSAGE"] = "TEXT_PASSAGE"
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)

    @model_validator(mode="after")
    def require_ordered_source_range(self) -> Self:
        """Ensure the absolute source range is non-empty and ordered."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        return self


EvidenceSourceLocator = Annotated[
    EvidencePdfPageLocator | EvidenceTextPassageLocator,
    Field(discriminator="kind"),
]


class EvidenceSpanResult(StrictContract):
    """Exact source location returned for an authorized citation."""

    evidence_span_id: UUID
    document_id: UUID
    document_version_id: UUID
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    exact_text: Annotated[str, StringConstraints(min_length=1)]
    text_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    locator: EvidenceSourceLocator
    viewer_url: str

    @model_validator(mode="after")
    def require_ordered_character_range(self) -> EvidenceSpanResult:
        """Ensure the end offset follows the start offset."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        return self


class StreamEventBase(StrictContract):
    """Fields carried by every research stream event."""

    schema_version: Literal["1.0"] = "1.0"
    research_request_id: UUID


class ResearchProgressEvent(StreamEventBase):
    """Progress emitted before validated answer content exists."""

    event: Literal["research.progress"] = "research.progress"
    stage: ResearchProgressStage


class AnswerStatementEvent(StreamEventBase):
    """One validated answer statement safe for rendering."""

    event: Literal["answer.statement"] = "answer.statement"
    statement_id: UUID
    order: int = Field(ge=0)
    text: Annotated[str, StringConstraints(min_length=1)]


class AnswerCitationEvent(StreamEventBase):
    """One validated relationship between a statement and evidence."""

    event: Literal["answer.citation"] = "answer.citation"
    statement_id: UUID
    evidence_span_id: UUID
    relationship: EvidenceRelationship
    display_order: int = Field(ge=0)


class CoverageStatus(StrEnum):
    """Evidence coverage reported after validation."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class AnswerCompletedEvent(StreamEventBase):
    """Terminal event for a validated answer."""

    event: Literal["answer.completed"] = "answer.completed"
    coverage: CoverageStatus


class AnswerInsufficientEvent(StreamEventBase):
    """Terminal event when eligible archive evidence is insufficient."""

    event: Literal["answer.insufficient"] = "answer.insufficient"
    coverage: Literal[CoverageStatus.INSUFFICIENT] = CoverageStatus.INSUFFICIENT
    message: SafeMessage


class ResearchFailedEvent(StreamEventBase):
    """Terminal event for a safely reported research failure."""

    event: Literal["research.failed"] = "research.failed"
    error: ErrorDetail
    retriable: bool


ResearchStreamEvent = Annotated[
    ResearchProgressEvent
    | AnswerStatementEvent
    | AnswerCitationEvent
    | AnswerCompletedEvent
    | AnswerInsufficientEvent
    | ResearchFailedEvent,
    Field(discriminator="event"),
]

PUBLIC_CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    ErrorResponse,
    DocumentMetadataInput,
    PastedTextDocumentInput,
    DocumentAccepted,
    ProcessingRunResult,
    ResearchRequestInput,
    EvidenceSpanResult,
    ResearchProgressEvent,
    AnswerStatementEvent,
    AnswerCitationEvent,
    AnswerCompletedEvent,
    AnswerInsufficientEvent,
    ResearchFailedEvent,
)
