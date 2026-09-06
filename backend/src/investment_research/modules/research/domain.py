"""Domain values and invariants for research requests and answers."""

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

QuestionText = Annotated[str, StringConstraints(min_length=1, max_length=4_000)]
AnswerText = Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
StatementText = Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
ConfigurationVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class TemporalMode(StrEnum):
    """Eligibility clocks available to a research request."""

    CURRENT = "CURRENT"
    PUBLIC_KNOWLEDGE = "PUBLIC_KNOWLEDGE"
    PERSONAL_ARCHIVE = "PERSONAL_ARCHIVE"


class AnswerState(StrEnum):
    """States in the target answer lifecycle."""

    REQUESTED = "REQUESTED"
    RETRIEVING = "RETRIEVING"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    STREAMED = "STREAMED"
    INSUFFICIENT = "INSUFFICIENT"
    REJECTED = "REJECTED"
    REGENERATED = "REGENERATED"

    def can_transition_to(self, target: Self) -> bool:
        """Return whether the target is a permitted next lifecycle state."""

        transitions = {
            AnswerState.REQUESTED: {AnswerState.RETRIEVING},
            AnswerState.RETRIEVING: {
                AnswerState.GENERATING,
                AnswerState.INSUFFICIENT,
            },
            AnswerState.GENERATING: {
                AnswerState.VALIDATING,
                AnswerState.REJECTED,
            },
            AnswerState.VALIDATING: {
                AnswerState.STREAMED,
                AnswerState.INSUFFICIENT,
                AnswerState.REJECTED,
            },
            AnswerState.REJECTED: {AnswerState.REGENERATED},
        }
        return target in transitions.get(self, set())


class CoverageStatus(StrEnum):
    """Archive-evidence coverage communicated by a terminal answer."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"


class StatementType(StrEnum):
    """Statement categories persisted by the M0.0 proof."""

    ARCHIVE_DERIVED = "ARCHIVE_DERIVED"


class StatementSupportStatus(StrEnum):
    """Validation status permitted for canonical M0.0 answer statements."""

    VALIDATED = "VALIDATED"


class ResearchScopeError(ValueError):
    """Raised when research data crosses the trusted workspace boundary."""


class ResearchRelationshipError(ValueError):
    """Raised when research records do not form one coherent answer result."""


class InvalidAnswerTransitionError(ValueError):
    """Raised when an answer skips or reverses its defined lifecycle."""


class ImmutableResearchModel(BaseModel):
    """Shared validation policy for immutable research snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResearchRequest(ImmutableResearchModel):
    """Immutable user question and its explicit effective archive context."""

    id: UUID
    workspace_id: UUID
    question: QuestionText
    document_ids: tuple[UUID, ...] = ()
    temporal_mode: TemporalMode
    cutoff_at: AwareDatetime | None = None
    created_at: AwareDatetime

    @field_validator("question")
    @classmethod
    def require_visible_question(cls, value: str) -> str:
        """Reject whitespace-only input without altering the user's exact question."""

        if not value.strip():
            raise ValueError("question must contain a non-whitespace character")
        return value

    @field_validator("document_ids")
    @classmethod
    def require_unique_document_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Reject ambiguous duplicate document filters while retaining caller order."""

        if len(value) != len(set(value)):
            raise ValueError("document_ids must be unique")
        return value

    @field_validator("cutoff_at", "created_at")
    @classmethod
    def normalize_request_times(cls, value: datetime | None) -> datetime | None:
        """Normalize request timestamps to UTC without inventing a cutoff."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_temporal_cutoff(self) -> Self:
        """Require cutoffs only for historical eligibility modes."""

        if self.temporal_mode is TemporalMode.CURRENT and self.cutoff_at is not None:
            raise ValueError("CURRENT temporal mode must not define cutoff_at")
        if self.temporal_mode is not TemporalMode.CURRENT and self.cutoff_at is None:
            raise ValueError("historical temporal mode requires cutoff_at")
        return self


class Answer(ImmutableResearchModel):
    """Versioned answer lifecycle snapshot for one research request."""

    id: UUID
    workspace_id: UUID
    research_request_id: UUID
    retrieval_run_id: UUID | None = None
    version_number: int = Field(ge=1)
    state: AnswerState
    coverage_status: CoverageStatus | None = None
    generator_version: ConfigurationVersion
    generation_config_version: ConfigurationVersion
    prompt_version: ConfigurationVersion
    rendered_text: AnswerText | None = None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @field_validator("rendered_text")
    @classmethod
    def require_visible_answer(cls, value: str | None) -> str | None:
        """Reject whitespace-only rendered answers without rewriting their text."""

        if value is not None and not value.strip():
            raise ValueError("rendered_text must contain a non-whitespace character")
        return value

    @field_validator("created_at", "completed_at")
    @classmethod
    def normalize_answer_times(cls, value: datetime | None) -> datetime | None:
        """Normalize answer timestamps to UTC without inventing completion."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_consistent_lifecycle_snapshot(self) -> Self:
        """Keep state, retrieval, coverage, rendered output, and timing consistent."""

        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")

        terminal_states = {
            AnswerState.STREAMED,
            AnswerState.INSUFFICIENT,
            AnswerState.REJECTED,
            AnswerState.REGENERATED,
        }
        if self.state in terminal_states:
            if self.completed_at is None:
                raise ValueError("terminal answer state requires completed_at")
        elif self.completed_at is not None:
            raise ValueError("non-terminal answer state must not define completed_at")

        retrieval_states = {
            AnswerState.GENERATING,
            AnswerState.VALIDATING,
            AnswerState.STREAMED,
            AnswerState.INSUFFICIENT,
            AnswerState.REJECTED,
            AnswerState.REGENERATED,
        }
        if self.state in retrieval_states:
            if self.retrieval_run_id is None:
                raise ValueError("answer state requires retrieval_run_id")
        elif self.retrieval_run_id is not None:
            raise ValueError("pre-generation answer state must not define retrieval_run_id")

        if self.state is AnswerState.STREAMED:
            if self.coverage_status not in {
                CoverageStatus.COMPLETE,
                CoverageStatus.PARTIAL,
                CoverageStatus.CONFLICTING,
            }:
                raise ValueError("STREAMED answer requires non-insufficient coverage")
            if self.rendered_text is None:
                raise ValueError("STREAMED answer requires rendered_text")
        elif self.state is AnswerState.INSUFFICIENT:
            if self.coverage_status is not CoverageStatus.INSUFFICIENT:
                raise ValueError("INSUFFICIENT answer requires insufficient coverage")
            if self.rendered_text is None:
                raise ValueError("INSUFFICIENT answer requires explicit rendered_text")
        elif self.coverage_status is not None or self.rendered_text is not None:
            raise ValueError("only user-visible terminal answers may contain output")
        return self


class AnswerStatement(ImmutableResearchModel):
    """Smallest independently supportable statement accepted for an answer."""

    id: UUID
    workspace_id: UUID
    answer_id: UUID
    ordinal: int = Field(ge=0)
    text: StatementText
    statement_type: StatementType
    support_status: StatementSupportStatus
    created_at: AwareDatetime

    @field_validator("text")
    @classmethod
    def require_visible_statement(cls, value: str) -> str:
        """Reject whitespace-only statements without altering validated prose."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize statement creation time to UTC."""

        return value.astimezone(UTC)
