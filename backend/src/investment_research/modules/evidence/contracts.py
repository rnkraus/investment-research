"""Typed application contracts for canonical server-side evidence validation."""

from datetime import UTC, datetime
from typing import Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_research.modules.documents.domain import (
    DocumentLifecycleState,
    SourceLocatorKind,
)
from investment_research.modules.evidence.domain import (
    ConfigurationVersion,
    EvidenceSpan,
    ValidationReason,
    ValidationRule,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import M1_STATEMENT_EVIDENCE_LIMIT
from investment_research.modules.research.domain import CoverageStatus, StatementText
from investment_research.modules.search.contracts import M1_CONTEXT_CHARACTER_LIMIT
from investment_research.modules.search.domain import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
)


class ImmutableEvidenceContract(BaseModel):
    """Reject unknown fields and preserve validated application boundary values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceValidationProfile(ImmutableEvidenceContract):
    """Versioned application-selected evidence-validation behavior."""

    validation_config_version: ConfigurationVersion


class StatementSupportInput(ImmutableEvidenceContract):
    """Bounded canonical text supplied to one semantic-support evaluation."""

    statement: StatementText
    evidence_texts: tuple[str, ...] = Field(
        min_length=1,
        max_length=M1_STATEMENT_EVIDENCE_LIMIT,
    )

    @field_validator("statement")
    @classmethod
    def require_visible_statement(cls, value: str) -> str:
        """Reject whitespace-only statements before provider evaluation."""

        if not value.strip():
            raise ValueError("support statement must contain a non-whitespace character")
        return value

    @field_validator("evidence_texts")
    @classmethod
    def require_bounded_visible_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject empty or oversized evidence before it reaches a model provider."""

        if any(not evidence_text.strip() for evidence_text in value):
            raise ValueError("support evidence must contain a non-whitespace character")
        if sum(len(evidence_text) for evidence_text in value) > M1_CONTEXT_CHARACTER_LIMIT:
            raise ValueError("support evidence exceeds the M1 context character limit")
        return value


class StatementSupportDecision(ImmutableEvidenceContract):
    """Typed provider decision that grants no citation or publication authority."""

    supported: bool


class StatementSupportResult(ImmutableEvidenceContract):
    """Support decision, usage, and an optional non-serialized local diagnostic."""

    decision: StatementSupportDecision
    usage: ProviderUsage
    diagnostic_reason: str | None = Field(
        default=None, min_length=1, max_length=500, exclude=True, repr=False
    )


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


M1_5_SUPPORT_CONTEXT_CHARACTER_LIMIT = 800
M1_5_SUPPORT_CONTEXT_POLICY_VERSION: Final = "m1-5-same-block-attribution-v1"
M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION: Final = "m1-5-document-prefix-attribution-v1"


class OriginalSupportContext(ImmutableEvidenceContract):
    """An exact bounded original prefix; never an extracted or trusted factual assertion."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    policy_version: Literal[
        "m1-5-same-block-attribution-v1", "m1-5-document-prefix-attribution-v1"
    ] = M1_5_SUPPORT_CONTEXT_POLICY_VERSION
    workspace_id: UUID | None = None
    processing_run_id: UUID | None = None
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=M1_5_SUPPORT_CONTEXT_CHARACTER_LIMIT, repr=False)

    @model_validator(mode="after")
    def require_exact_visible_range(self) -> Self:
        """Keep unchanged ranges and require scoped processing identity for document context."""

        if self.end_character - self.start_character != len(self.text) or not self.text.strip():
            raise ValueError("support context must contain visible text matching its range")
        if self.policy_version == M1_5_DOCUMENT_ATTRIBUTION_POLICY_VERSION and (
            self.workspace_id is None or self.processing_run_id is None
        ):
            raise ValueError("document attribution requires workspace and processing identity")
        return self


class CanonicalEvidenceSource(ImmutableEvidenceContract):
    """Scoped canonical state needed to revalidate one retrieved candidate."""

    workspace_id: UUID
    retrieval_candidate_id: UUID
    retrieval_run_id: UUID
    origin: RetrievalOrigin
    selection_rank: int | None = Field(default=None, ge=1)
    chunk_id: UUID
    chunk_text: str = Field(min_length=1, max_length=100_000)
    chunk_set_is_active: bool
    chunk_set_lexical_config_version: ConfigurationVersion
    processing_state: ProcessingState
    processing_lexical_config_version: ConfigurationVersion
    processing_embedding_config_version: ConfigurationVersion
    processing_dense_index_config_version: ConfigurationVersion
    processing_run_id: UUID | None = None
    document_attribution_context: OriginalSupportContext | None = None
    document_id: UUID
    document_lifecycle_state: DocumentLifecycleState
    document_current_version_id: UUID | None
    document_version_id: UUID
    version_available_at: AwareDatetime | None
    version_captured_at: AwareDatetime
    structure_block_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    structure_block_text: str = Field(min_length=1, max_length=100_000)
    structure_block_start_character: int = Field(ge=0)
    structure_block_end_character: int = Field(gt=0)
    retrieval_filters: RetrievalFilters
    retrieval_mode: RetrievalMode
    retrieval_lexical_config_version: ConfigurationVersion
    retrieval_embedding_config_version: ConfigurationVersion | None = None
    retrieval_dense_index_config_version: ConfigurationVersion | None = None

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

    @model_validator(mode="after")
    def require_media_appropriate_source_range(self) -> Self:
        """Require one valid block locator before evidence validation can use it."""

        if self.structure_block_end_character <= self.structure_block_start_character:
            raise ValueError("structure block source range must be ordered")
        if self.structure_block_end_character - self.structure_block_start_character != len(
            self.structure_block_text
        ):
            raise ValueError("structure block source range must match its text length")
        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE evidence source requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE evidence source must not define page_number")
        return self


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
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    source_start_character: int | None = Field(default=None, ge=0)
    source_end_character: int | None = Field(default=None, gt=0)
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
        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE reference requires page_number")
            if self.source_start_character is not None or self.source_end_character is not None:
                raise ValueError("PDF_PAGE reference must not define text-passage offsets")
        else:
            if self.page_number is not None:
                raise ValueError("TEXT_PASSAGE reference must not define page_number")
            if self.source_start_character is None or self.source_end_character is None:
                raise ValueError("TEXT_PASSAGE reference requires source offsets")
            if self.source_end_character <= self.source_start_character:
                raise ValueError("source_end_character must follow source_start_character")
            if self.source_end_character - self.source_start_character != len(self.exact_text):
                raise ValueError("text-passage source offsets must match exact_text length")
        return self


class ContextualStatementEvidence(ImmutableEvidenceContract):
    """Pair a citation with context under an explicitly versioned original-source policy."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    reference: ValidatedEvidenceReference
    attribution_context: OriginalSupportContext

    @model_validator(mode="after")
    def require_same_original_source(self) -> Self:
        """Do not attach one document's header to another document's quote."""

        reference = self.reference
        context = self.attribution_context
        if (
            reference.document_id,
            reference.document_version_id,
        ) != (context.document_id, context.document_version_id):
            raise ValueError("support context must belong to the cited original version")
        if context.policy_version == M1_5_SUPPORT_CONTEXT_POLICY_VERSION and (
            reference.structure_block_id != context.structure_block_id
        ):
            raise ValueError("same-block support context must belong to the cited original block")
        return self


class ContextualStatementSupportInput(ImmutableEvidenceContract):
    """Prepared M1.5 input whose context cannot replace exact cited factual evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    statement: StatementText
    evidence: tuple[ContextualStatementEvidence, ...] = Field(
        min_length=1, max_length=M1_STATEMENT_EVIDENCE_LIMIT
    )

    @model_validator(mode="after")
    def require_bounded_unique_evidence(self) -> Self:
        """Retain the M1 total text bound and distinct original citation locations."""

        if not self.statement.strip():
            raise ValueError("support statement must contain a non-whitespace character")
        if any(not item.reference.exact_text.strip() for item in self.evidence):
            raise ValueError("contextual support evidence must contain visible quote text")
        if len({item.attribution_context.policy_version for item in self.evidence}) != 1:
            raise ValueError("contextual support input must use one context policy")
        if (
            sum(
                len(item.reference.exact_text) + len(item.attribution_context.text)
                for item in self.evidence
            )
            > M1_CONTEXT_CHARACTER_LIMIT
        ):
            raise ValueError("contextual support input exceeds the M1 context character limit")
        keys = tuple(
            (
                item.reference.document_version_id,
                item.reference.structure_block_id,
                item.reference.start_character,
                item.reference.end_character,
            )
            for item in self.evidence
        )
        if len(keys) != len(set(keys)):
            raise ValueError("contextual support evidence must have distinct original ranges")
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
