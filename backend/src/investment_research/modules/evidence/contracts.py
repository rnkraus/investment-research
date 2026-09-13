"""Typed application contracts for canonical server-side evidence validation."""

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.evidence.domain import (
    ConfigurationVersion,
    EvidenceSpan,
    ValidationReason,
    ValidationRule,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.domain import CoverageStatus, StatementText
from investment_research.modules.search.domain import RetrievalFilters, RetrievalOrigin


class ImmutableEvidenceContract(BaseModel):
    """Reject unknown fields and preserve validated application boundary values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceValidationProfile(ImmutableEvidenceContract):
    """Versioned application-selected evidence-validation behavior."""

    validation_config_version: ConfigurationVersion


class CanonicalEvidenceSpanSource(ImmutableEvidenceContract):
    """Canonical state needed to authorize one persisted citation span."""

    span: EvidenceSpan
    document_id: UUID
    document_lifecycle_state: DocumentLifecycleState
    document_current_version_id: UUID | None
    linked_to_streamed_answer: bool


class ResolvedEvidenceSpan(ImmutableEvidenceContract):
    """Authorized citation location safe for transport-layer mapping."""

    span: EvidenceSpan
    document_id: UUID


class CanonicalEvidenceSource(ImmutableEvidenceContract):
    """Scoped canonical state needed to revalidate one retrieved candidate."""

    workspace_id: UUID
    retrieval_candidate_id: UUID
    retrieval_run_id: UUID
    origin: RetrievalOrigin
    chunk_id: UUID
    chunk_text: str = Field(min_length=1, max_length=100_000)
    chunk_set_is_active: bool
    chunk_set_lexical_config_version: ConfigurationVersion
    processing_state: ProcessingState
    processing_lexical_config_version: ConfigurationVersion
    document_id: UUID
    document_lifecycle_state: DocumentLifecycleState
    document_current_version_id: UUID | None
    document_version_id: UUID
    version_available_at: AwareDatetime | None
    version_captured_at: AwareDatetime
    structure_block_id: UUID
    page_number: int = Field(ge=1)
    structure_block_text: str = Field(min_length=1, max_length=100_000)
    retrieval_filters: RetrievalFilters
    retrieval_lexical_config_version: ConfigurationVersion

    @field_validator("chunk_text", "structure_block_text")
    @classmethod
    def require_visible_source_text(cls, value: str) -> str:
        """Reject whitespace-only canonical source text without rewriting it."""

        if not value.strip():
            raise ValueError("canonical source text must contain a non-whitespace character")
        return value

    @field_validator("version_available_at", "version_captured_at")
    @classmethod
    def normalize_source_times(cls, value: datetime | None) -> datetime | None:
        """Normalize canonical eligibility timestamps to UTC."""

        return None if value is None else value.astimezone(UTC)


class ValidationFinding(ImmutableEvidenceContract):
    """One answer- or statement-level server-side rule result."""

    rule: ValidationRule
    statement_ordinal: int | None = Field(default=None, ge=0)
    passed: bool
    reason: ValidationReason | None = None

    @model_validator(mode="after")
    def require_reason_only_for_failure(self) -> Self:
        """Require a safe reason for failures and omit one for passing rules."""

        if self.passed and self.reason is not None:
            raise ValueError("passing validation must not define reason")
        if not self.passed and self.reason is None:
            raise ValueError("failed validation requires reason")
        return self


class ValidatedEvidenceReference(ImmutableEvidenceContract):
    """Exact source identity proven against canonical state."""

    retrieval_candidate_id: UUID
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    page_number: int = Field(ge=1)
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    exact_text: str = Field(min_length=1, max_length=50_000)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def require_matching_reference_length(self) -> Self:
        """Keep the canonical text range and exact text length aligned."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        if self.end_character - self.start_character != len(self.exact_text):
            raise ValueError("validated offsets must match exact_text length")
        return self


class ValidatedStatementProposal(ImmutableEvidenceContract):
    """One generated statement accepted with all of its canonical evidence."""

    ordinal: int = Field(ge=0)
    text: StatementText
    evidence: tuple[ValidatedEvidenceReference, ...] = Field(min_length=1, max_length=10)

    @field_validator("evidence")
    @classmethod
    def require_unique_evidence(
        cls,
        value: tuple[ValidatedEvidenceReference, ...],
    ) -> tuple[ValidatedEvidenceReference, ...]:
        """Reject duplicate canonical spans before persistence constraints are reached."""

        keys = tuple(
            (
                reference.document_version_id,
                reference.structure_block_id,
                reference.start_character,
                reference.end_character,
            )
            for reference in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("validated statement evidence must be unique")
        return value


class EvidenceValidationDecision(ImmutableEvidenceContract):
    """Safe result of validating a complete generated answer proposal."""

    accepted: bool
    coverage_status: CoverageStatus | None
    statements: tuple[ValidatedStatementProposal, ...] = Field(max_length=50)
    findings: tuple[ValidationFinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_consistent_decision(self) -> Self:
        """Prevent failed or insufficient decisions from exposing accepted statements."""

        all_rules_passed = all(finding.passed for finding in self.findings)
        if self.accepted != all_rules_passed:
            raise ValueError("accepted must match the complete validation result")
        if not self.accepted:
            if self.coverage_status is not None or self.statements:
                raise ValueError("rejected validation must not expose answer content")
            return self
        if self.coverage_status is None:
            raise ValueError("accepted validation requires coverage_status")
        if self.coverage_status is CoverageStatus.INSUFFICIENT:
            if self.statements:
                raise ValueError("insufficient validation must not expose statements")
        elif not self.statements:
            raise ValueError("answered validation requires at least one statement")
        return self
