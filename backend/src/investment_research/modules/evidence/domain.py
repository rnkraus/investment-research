"""Domain values and invariants for exact evidence and validation records."""

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
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

EvidenceText = Annotated[str, StringConstraints(min_length=1, max_length=50_000)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
ValidationReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
]
ConfigurationVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class EvidenceRelationship(StrEnum):
    """Reviewed relationships between a statement and a source span."""

    SUPPORTS = "SUPPORTS"
    CONFLICTS = "CONFLICTS"


class ValidationRule(StrEnum):
    """Server-side evidence rules recorded by the M0.0 validator."""

    WORKSPACE_SCOPE = "WORKSPACE_SCOPE"
    ACTIVE_DOCUMENT_VERSION = "ACTIVE_DOCUMENT_VERSION"
    TEMPORAL_ELIGIBILITY = "TEMPORAL_ELIGIBILITY"
    RETRIEVAL_CANDIDATE = "RETRIEVAL_CANDIDATE"
    EXACT_SOURCE_SPAN = "EXACT_SOURCE_SPAN"
    STATEMENT_SUPPORT = "STATEMENT_SUPPORT"
    COVERAGE = "COVERAGE"


class EvidenceScopeError(ValueError):
    """Raised when evidence data crosses the trusted workspace boundary."""


class EvidenceRelationshipError(ValueError):
    """Raised when evidence records do not form a coherent result."""


class ImmutableEvidenceModel(BaseModel):
    """Shared validation policy for immutable evidence snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceSpan(ImmutableEvidenceModel):
    """Exact immutable source text anchored within one parsed structure block."""

    id: UUID
    workspace_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(ge=1)
    source_start_character: int | None = Field(ge=0)
    source_end_character: int | None = Field(gt=0)
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    exact_text: EvidenceText
    text_sha256: Sha256Hex
    created_at: AwareDatetime

    @field_validator("exact_text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only spans without altering exact source text."""

        if not value.strip():
            raise ValueError("exact_text must contain a non-whitespace character")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize evidence creation time to UTC."""

        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_exact_source_identity(self) -> Self:
        """Require exact block text and one valid immutable source-locator shape."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        if self.end_character - self.start_character != len(self.exact_text):
            raise ValueError("source offsets must match exact_text length")
        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE evidence requires page_number")
            if self.source_start_character is not None or self.source_end_character is not None:
                raise ValueError("PDF_PAGE evidence must not define text-passage offsets")
        else:
            if self.page_number is not None:
                raise ValueError("TEXT_PASSAGE evidence must not define page_number")
            if self.source_start_character is None or self.source_end_character is None:
                raise ValueError("TEXT_PASSAGE evidence requires source offsets")
            if self.source_end_character <= self.source_start_character:
                raise ValueError("source_end_character must be greater than source_start_character")
            if self.source_end_character - self.source_start_character != len(self.exact_text):
                raise ValueError("text-passage offsets must match exact_text length")
        expected_hash = sha256(self.exact_text.encode("utf-8")).hexdigest()
        if self.text_sha256 != expected_hash:
            raise ValueError("text_sha256 must match exact_text")
        return self


class StatementEvidence(ImmutableEvidenceModel):
    """Ordered supporting or conflicting source relationship for one statement."""

    id: UUID
    workspace_id: UUID
    answer_statement_id: UUID
    evidence_span_id: UUID
    relationship: EvidenceRelationship
    display_order: int = Field(ge=0)
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize relationship creation time to UTC."""

        return value.astimezone(UTC)


class AnswerValidation(ImmutableEvidenceModel):
    """Immutable result of applying one versioned server-side validation rule."""

    id: UUID
    workspace_id: UUID
    answer_id: UUID
    answer_statement_id: UUID | None = None
    rule: ValidationRule
    passed: bool
    reason: ValidationReason | None = None
    validation_config_version: ConfigurationVersion
    validated_at: AwareDatetime

    @field_validator("validated_at")
    @classmethod
    def normalize_validated_at(cls, value: datetime) -> datetime:
        """Normalize validation time to UTC."""

        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_failure_reason(self) -> Self:
        """Require a safe diagnostic reason whenever a rule fails."""

        if not self.passed and self.reason is None:
            raise ValueError("failed validation requires reason")
        return self
