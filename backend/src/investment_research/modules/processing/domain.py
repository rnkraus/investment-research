"""Domain values and invariants for document processing and lexical chunks."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from investment_research.modules.documents.domain import SourceLocatorKind

ConfigurationVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ProcessingText = Annotated[str, StringConstraints(min_length=1)]
SafeErrorSummary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class ProcessingState(StrEnum):
    """States implemented by the M0.0 lexical processing lifecycle."""

    CAPTURED = "CAPTURED"
    PARSING = "PARSING"
    LEXICAL_READY = "LEXICAL_READY"
    FAILED = "FAILED"


class StructureBlockType(StrEnum):
    """Page-aware source structures created from parsed document content."""

    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"


class ProcessingScopeError(ValueError):
    """Raised when processing data crosses the trusted workspace boundary."""


class ProcessingRelationshipError(ValueError):
    """Raised when processing records do not describe one source version and run."""


class InvalidProcessingTransitionError(ValueError):
    """Raised when a processing run attempts a forbidden lifecycle transition."""


class ProcessingLeaseActiveError(InvalidProcessingTransitionError):
    """Raised when another processing attempt still owns an active lease."""


class ProcessingLeaseOwnershipError(InvalidProcessingTransitionError):
    """Raised when an attempt does not own the run's current lease."""


class ProcessingAttemptLimitError(InvalidProcessingTransitionError):
    """Raised when no additional execution attempt may claim a processing run."""


class ImmutableProcessingModel(BaseModel):
    """Shared validation policy for immutable processing snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProcessingRun(ImmutableProcessingModel):
    """One versioned attempt to process an immutable document version."""

    id: UUID
    workspace_id: UUID
    document_version_id: UUID
    state: ProcessingState
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    chunker_version: ConfigurationVersion
    chunking_config_version: ConfigurationVersion
    lexical_config_version: ConfigurationVersion
    created_at: AwareDatetime
    attempt_count: int = Field(default=0, ge=0)
    lease_id: UUID | None = None
    lease_expires_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error_summary: SafeErrorSummary | None = None

    @field_validator("created_at", "lease_expires_at", "started_at", "completed_at")
    @classmethod
    def normalize_run_times(cls, value: datetime | None) -> datetime | None:
        """Normalize known run times to UTC without inventing missing events."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_consistent_state_timestamps(self) -> Self:
        """Keep lifecycle state, timestamps, and failure details consistent."""

        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at must not precede created_at")
        if self.completed_at is not None and (
            self.started_at is None or self.completed_at < self.started_at
        ):
            raise ValueError("completed_at must not precede started_at")

        has_complete_lease = self.lease_id is not None and self.lease_expires_at is not None
        has_partial_lease = (self.lease_id is None) != (self.lease_expires_at is None)
        if has_partial_lease:
            raise ValueError("lease_id and lease_expires_at must be set together")

        if self.state is ProcessingState.CAPTURED:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("CAPTURED run must not have processing timestamps")
            if self.attempt_count != 0 or has_complete_lease:
                raise ValueError("CAPTURED run must not have execution-attempt metadata")
        elif self.state is ProcessingState.PARSING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("PARSING run requires only started_at")
            if self.attempt_count < 1 or self.lease_id is None or self.lease_expires_at is None:
                raise ValueError("PARSING run requires an attempt and complete lease")
            if self.lease_expires_at <= self.started_at:
                raise ValueError("lease_expires_at must be later than started_at")
        elif self.started_at is None or self.completed_at is None:
            raise ValueError("terminal processing state requires start and completion times")
        elif self.attempt_count < 1 or has_complete_lease:
            raise ValueError("terminal processing state requires attempts and no lease")

        if self.state is ProcessingState.FAILED:
            if self.error_summary is None:
                raise ValueError("FAILED run requires error_summary")
        elif self.error_summary is not None:
            raise ValueError("only FAILED run may contain error_summary")
        return self

    def claim_for_processing(
        self,
        *,
        lease_id: UUID,
        claimed_at: datetime,
        lease_expires_at: datetime,
        maximum_attempts: int,
    ) -> Self:
        """Claim a captured run or reclaim one whose previous lease expired."""

        claimed_at = self._aware_utc(claimed_at, name="claimed_at")
        lease_expires_at = self._aware_utc(
            lease_expires_at,
            name="lease_expires_at",
        )
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be positive")
        if lease_expires_at <= claimed_at:
            raise ValueError("lease_expires_at must be later than claimed_at")
        if self.state not in {ProcessingState.CAPTURED, ProcessingState.PARSING}:
            raise InvalidProcessingTransitionError(
                f"cannot claim processing run in {self.state.value}"
            )
        if self.attempt_count >= maximum_attempts:
            raise ProcessingAttemptLimitError("processing attempt limit reached")
        if (
            self.state is ProcessingState.PARSING
            and self.lease_expires_at is not None
            and self.lease_expires_at > claimed_at
        ):
            raise ProcessingLeaseActiveError("processing run already has an active lease")

        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.PARSING,
                "attempt_count": self.attempt_count + 1,
                "lease_id": lease_id,
                "lease_expires_at": lease_expires_at,
                "started_at": self.started_at or claimed_at,
            }
        )

    def mark_lexical_ready(self, *, lease_id: UUID, completed_at: datetime) -> Self:
        """Complete parsing after canonical blocks and chunks are ready to commit."""

        self._require_state(ProcessingState.PARSING, ProcessingState.LEXICAL_READY)
        self._require_lease_owner(lease_id)
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.LEXICAL_READY,
                "lease_id": None,
                "lease_expires_at": None,
                "completed_at": completed_at,
            }
        )

    def mark_failed(
        self,
        *,
        lease_id: UUID,
        completed_at: datetime,
        error_summary: str,
    ) -> Self:
        """Terminate active parsing with one bounded, caller-sanitized summary."""

        self._require_state(ProcessingState.PARSING, ProcessingState.FAILED)
        self._require_lease_owner(lease_id)
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.FAILED,
                "lease_id": None,
                "lease_expires_at": None,
                "completed_at": completed_at,
                "error_summary": error_summary,
            }
        )

    def expire_lease_for_retry(self, *, lease_id: UUID, expired_at: datetime) -> Self:
        """Make the owned lease immediately reclaimable after a transient failure."""

        self._require_state(ProcessingState.PARSING, ProcessingState.PARSING)
        self._require_lease_owner(lease_id)
        expired_at = self._aware_utc(expired_at, name="expired_at")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "lease_expires_at": expired_at,
            }
        )

    def mark_attempts_exhausted(
        self,
        *,
        completed_at: datetime,
        error_summary: str,
    ) -> Self:
        """Fail an abandoned run when its expired lease used the last attempt."""

        self._require_state(ProcessingState.PARSING, ProcessingState.FAILED)
        completed_at = self._aware_utc(completed_at, name="completed_at")
        if self.lease_expires_at is None or self.lease_expires_at > completed_at:
            raise ProcessingLeaseActiveError("processing run still has an active lease")
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.FAILED,
                "lease_id": None,
                "lease_expires_at": None,
                "completed_at": completed_at,
                "error_summary": error_summary,
            }
        )

    def _require_state(
        self,
        expected: ProcessingState,
        target: ProcessingState,
    ) -> None:
        """Require one explicit forward transition from the current snapshot."""

        if self.state is not expected:
            raise InvalidProcessingTransitionError(
                f"cannot transition processing run from {self.state.value} to {target.value}"
            )

    def _require_lease_owner(self, lease_id: UUID) -> None:
        """Require the caller to own the current attempt before changing its state."""

        if self.lease_id != lease_id:
            raise ProcessingLeaseOwnershipError("processing lease is not owned by this attempt")

    @staticmethod
    def _aware_utc(value: datetime, *, name: str) -> datetime:
        """Validate and normalize a transition timestamp before comparing it."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)


class StructureBlock(ImmutableProcessingModel):
    """Canonical parsed text anchored to one media-appropriate source range."""

    id: UUID
    workspace_id: UUID
    processing_run_id: UUID
    document_version_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(ge=1)
    block_type: StructureBlockType
    ordinal: int = Field(ge=0)
    text: ProcessingText
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    created_at: AwareDatetime

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only blocks without altering exact source text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize structure creation time to UTC."""

        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_valid_source_locator(self) -> Self:
        """Require ordered offsets and the exact fields for the locator kind."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        if self.end_character - self.start_character != len(self.text):
            raise ValueError("source offsets must match text length")
        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE block requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE block must not define page_number")
        return self


class ChunkSet(ImmutableProcessingModel):
    """Versioned chunking result for one processing run and document version."""

    id: UUID
    workspace_id: UUID
    processing_run_id: UUID
    document_version_id: UUID
    chunker_version: ConfigurationVersion
    chunking_config_version: ConfigurationVersion
    lexical_config_version: ConfigurationVersion
    is_active: bool
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize chunk-set creation time to UTC."""

        return value.astimezone(UTC)


class Chunk(ImmutableProcessingModel):
    """Retrieval unit anchored to one structure block in the M0.0 subset."""

    id: UUID
    workspace_id: UUID
    processing_run_id: UUID
    document_version_id: UUID
    chunk_set_id: UUID
    structure_block_id: UUID
    ordinal: int = Field(ge=0)
    text: ProcessingText
    token_count: int = Field(gt=0)
    created_at: AwareDatetime

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only chunks without altering retrieval text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize chunk creation time to UTC."""

        return value.astimezone(UTC)
