"""Versioned HTTP and stream contracts for the M0.0 API surface."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

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
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    PROCESSING_FAILED = "PROCESSING_FAILED"
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


class DocumentAccepted(StrictContract):
    """Identifiers returned after immutable capture has been accepted."""

    document_id: UUID
    document_version_id: UUID
    processing_run_id: UUID
    state: Literal["CAPTURED"] = "CAPTURED"
    processing_status_url: str


class ProcessingState(StrEnum):
    """M0.0 processing states shared by status responses."""

    CAPTURED = "CAPTURED"
    PARSING = "PARSING"
    LEXICAL_READY = "LEXICAL_READY"
    FAILED = "FAILED"


class ProcessingRunResult(StrictContract):
    """Safe processing status visible to the owning workspace."""

    processing_run_id: UUID
    document_version_id: UUID
    state: ProcessingState
    error_summary: SafeMessage | None = None


class TemporalMode(StrEnum):
    """Supported meanings of a research time cutoff."""

    PUBLIC_KNOWLEDGE = "PUBLIC_KNOWLEDGE"
    PERSONAL_ARCHIVE = "PERSONAL_ARCHIVE"


class ResearchRequestContext(StrictContract):
    """Explicit archive filters supplied with a research question."""

    document_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    temporal_mode: TemporalMode = TemporalMode.PERSONAL_ARCHIVE
    cutoff_at: AwareDatetime | None = None


class ResearchRequestInput(StrictContract):
    """Question and explicit context accepted by the research endpoint."""

    question: Question
    context: ResearchRequestContext = Field(default_factory=ResearchRequestContext)


class EvidenceRelationship(StrEnum):
    """How a source span relates to an answer statement."""

    SUPPORTS = "SUPPORTS"
    CONFLICTS = "CONFLICTS"


class EvidenceSpanResult(StrictContract):
    """Exact source location returned for an authorized citation."""

    evidence_span_id: UUID
    document_id: UUID
    document_version_id: UUID
    page: int = Field(ge=1)
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    exact_text: Annotated[str, StringConstraints(min_length=1)]
    text_sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
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
    stage: Literal["RETRIEVING", "GENERATING", "VALIDATING"]


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
