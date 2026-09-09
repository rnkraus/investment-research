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
    """Page-aware source structures emitted by the bounded PDF parser."""

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
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error_summary: SafeErrorSummary | None = None

    @field_validator("created_at", "started_at", "completed_at")
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

        if self.state is ProcessingState.CAPTURED:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("CAPTURED run must not have processing timestamps")
        elif self.state is ProcessingState.PARSING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("PARSING run requires only started_at")
        elif self.started_at is None or self.completed_at is None:
            raise ValueError("terminal processing state requires start and completion times")

        if self.state is ProcessingState.FAILED:
            if self.error_summary is None:
                raise ValueError("FAILED run requires error_summary")
        elif self.error_summary is not None:
            raise ValueError("only FAILED run may contain error_summary")
        return self

    def start_parsing(self, *, started_at: datetime) -> Self:
        """Advance one captured run to its active parsing state."""

        self._require_state(ProcessingState.CAPTURED, ProcessingState.PARSING)
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.PARSING,
                "started_at": started_at,
            }
        )

    def mark_lexical_ready(self, *, completed_at: datetime) -> Self:
        """Complete parsing after canonical blocks and chunks are ready to commit."""

        self._require_state(ProcessingState.PARSING, ProcessingState.LEXICAL_READY)
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.LEXICAL_READY,
                "completed_at": completed_at,
            }
        )

    def mark_failed(self, *, completed_at: datetime, error_summary: str) -> Self:
        """Terminate active parsing with one bounded, caller-sanitized summary."""

        self._require_state(ProcessingState.PARSING, ProcessingState.FAILED)
        return type(self).model_validate(
            {
                **self.model_dump(),
                "state": ProcessingState.FAILED,
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


class StructureBlock(ImmutableProcessingModel):
    """Canonical page-aware parsed text anchored to source offsets."""

    id: UUID
    workspace_id: UUID
    processing_run_id: UUID
    document_version_id: UUID
    page_number: int = Field(ge=1)
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
    def require_ordered_source_offsets(self) -> Self:
        """Require a non-empty source range for the block text."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
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
