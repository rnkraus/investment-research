"""Typed application contracts for research retrieval orchestration."""

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.evidence.domain import (
    AnswerValidation,
    EvidenceSpan,
    StatementEvidence,
)
from investment_research.modules.research.domain import (
    Answer,
    AnswerState,
    AnswerStatement,
    ConfigurationVersion,
    CoverageStatus,
    QuestionText,
    ResearchRequest,
    StatementText,
    TemporalMode,
)
from investment_research.modules.search.contracts import LexicalSearchHit
from investment_research.modules.search.domain import (
    QueryText,
    RetrievalCandidate,
    RetrievalOrigin,
    RetrievalRun,
)


class ImmutableResearchContract(BaseModel):
    """Reject unknown fields and preserve validated application boundary values."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResearchRetrievalInput(ImmutableResearchContract):
    """One bounded question and explicit archive context submitted for retrieval."""

    question: QueryText
    document_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    temporal_mode: TemporalMode = TemporalMode.CURRENT
    cutoff_at: AwareDatetime | None = None

    @field_validator("question")
    @classmethod
    def require_visible_question(cls, value: str) -> str:
        """Reject whitespace-only input without rewriting the exact question."""

        if not value.strip():
            raise ValueError("question must contain a non-whitespace character")
        return value

    @field_validator("document_ids")
    @classmethod
    def require_unique_document_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Reject duplicate filters while retaining the caller's stable order."""

        if len(value) != len(set(value)):
            raise ValueError("document_ids must be unique")
        return value

    @field_validator("cutoff_at")
    @classmethod
    def normalize_cutoff_at(cls, value: datetime | None) -> datetime | None:
        """Normalize a historical cutoff to UTC without inventing a value."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_temporal_cutoff(self) -> Self:
        """Require cutoffs only for historical eligibility modes."""

        if self.temporal_mode is TemporalMode.CURRENT and self.cutoff_at is not None:
            raise ValueError("CURRENT temporal mode must not define cutoff_at")
        if self.temporal_mode is not TemporalMode.CURRENT and self.cutoff_at is None:
            raise ValueError("historical temporal mode requires cutoff_at")
        return self


class ResearchRetrievalResult(ImmutableResearchContract):
    """Committed request, retrieval record, candidates, and their canonical hit data."""

    request: ResearchRequest
    retrieval_run: RetrievalRun
    candidates: tuple[RetrievalCandidate, ...]
    hits: tuple[LexicalSearchHit, ...]

    @model_validator(mode="after")
    def require_consistent_result(self) -> Self:
        """Keep persisted candidates aligned with the exact ordered adapter hits."""

        if (
            self.retrieval_run.workspace_id != self.request.workspace_id
            or self.retrieval_run.research_request_id != self.request.id
            or self.retrieval_run.query_text != self.request.question
        ):
            raise ValueError("retrieval run does not belong to the research request")
        if len(self.candidates) != len(self.hits):
            raise ValueError("each retrieval candidate must retain one source hit")

        for expected_rank, (candidate, hit) in enumerate(
            zip(self.candidates, self.hits, strict=True),
            start=1,
        ):
            if (
                candidate.workspace_id != self.request.workspace_id
                or candidate.retrieval_run_id != self.retrieval_run.id
                or candidate.origin is not RetrievalOrigin.LEXICAL
                or candidate.rank != expected_rank
                or candidate.chunk_id != hit.chunk_id
                or candidate.raw_score != hit.raw_score
            ):
                raise ValueError("retrieval candidate does not match its ordered lexical hit")
        return self


class AnswerGenerationProfile(ImmutableResearchContract):
    """Versioned generator, configuration, and prompt selected by the application."""

    generator_version: ConfigurationVersion
    generation_config_version: ConfigurationVersion
    prompt_version: ConfigurationVersion


class AnswerGenerationSource(ImmutableResearchContract):
    """One ordered retrieval candidate exposed to an untrusted answer generator."""

    retrieval_candidate_id: UUID
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    text: str = Field(min_length=1, max_length=100_000)

    @field_validator("text")
    @classmethod
    def require_visible_source_text(cls, value: str) -> str:
        """Reject whitespace-only source text without changing canonical content."""

        if not value.strip():
            raise ValueError("source text must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> Self:
        """Keep generator context aligned with the canonical source medium."""

        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE generation source requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE generation source must not define page_number")
        return self


class AnswerGenerationInput(ImmutableResearchContract):
    """Bounded question and ordered candidate sources supplied to a generator."""

    research_request_id: UUID
    question: QuestionText
    sources: tuple[AnswerGenerationSource, ...] = Field(max_length=100)

    @field_validator("question")
    @classmethod
    def require_visible_generation_question(cls, value: str) -> str:
        """Reject whitespace-only questions while preserving the user's exact text."""

        if not value.strip():
            raise ValueError("question must contain a non-whitespace character")
        return value

    @field_validator("sources")
    @classmethod
    def require_unique_candidate_sources(
        cls,
        value: tuple[AnswerGenerationSource, ...],
    ) -> tuple[AnswerGenerationSource, ...]:
        """Prevent one retrieval candidate from appearing more than once."""

        candidate_ids = tuple(source.retrieval_candidate_id for source in value)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("generation sources must have unique retrieval_candidate_id values")
        return value


class UnvalidatedEvidenceReference(ImmutableResearchContract):
    """Generator-proposed exact text range inside one retrieved candidate."""

    retrieval_candidate_id: UUID
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)
    exact_text: str = Field(min_length=1, max_length=50_000)

    @field_validator("exact_text")
    @classmethod
    def require_visible_evidence_text(cls, value: str) -> str:
        """Reject whitespace-only proposed evidence without altering its text."""

        if not value.strip():
            raise ValueError("exact_text must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def require_matching_reference_length(self) -> Self:
        """Require offsets that could identify exactly the proposed text."""

        if self.end_character <= self.start_character:
            raise ValueError("end_character must be greater than start_character")
        if self.end_character - self.start_character != len(self.exact_text):
            raise ValueError("reference offsets must match exact_text length")
        return self


class UnvalidatedGeneratedStatement(ImmutableResearchContract):
    """One generator-proposed statement and its still-untrusted evidence references."""

    text: StatementText
    evidence: tuple[UnvalidatedEvidenceReference, ...] = Field(min_length=1, max_length=10)

    @field_validator("text")
    @classmethod
    def require_visible_statement_text(cls, value: str) -> str:
        """Reject whitespace-only generated statements without rewriting their text."""

        if not value.strip():
            raise ValueError("statement text must contain a non-whitespace character")
        return value

    @field_validator("evidence")
    @classmethod
    def require_unique_evidence_references(
        cls,
        value: tuple[UnvalidatedEvidenceReference, ...],
    ) -> tuple[UnvalidatedEvidenceReference, ...]:
        """Reject duplicate proposed candidate ranges before canonical validation."""

        keys = tuple(
            (
                reference.retrieval_candidate_id,
                reference.start_character,
                reference.end_character,
            )
            for reference in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("generated statement evidence references must be unique")
        return value


class UnvalidatedGeneratedAnswer(ImmutableResearchContract):
    """Schema-valid generator output that must not escape before evidence validation."""

    coverage_status: CoverageStatus
    rendered_text: str = Field(min_length=1, max_length=100_000)
    statements: tuple[UnvalidatedGeneratedStatement, ...] = Field(max_length=50)
    missing_information: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("rendered_text", "missing_information")
    @classmethod
    def require_visible_generated_text(cls, value: str | None) -> str | None:
        """Reject whitespace-only user-visible generator text."""

        if value is not None and not value.strip():
            raise ValueError("generated text must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def require_consistent_coverage_shape(self) -> Self:
        """Keep an insufficient result statement-free and answered results non-empty."""

        if self.coverage_status is CoverageStatus.INSUFFICIENT:
            if self.statements:
                raise ValueError("insufficient generation must not contain statements")
            if self.missing_information is None:
                raise ValueError("insufficient generation requires missing_information")
        else:
            if not self.statements:
                raise ValueError("non-insufficient generation requires at least one statement")
            if self.missing_information is not None:
                raise ValueError("non-insufficient generation must not define missing_information")
        return self


class ValidatedAnswerResult(ImmutableResearchContract):
    """Terminal canonical answer aggregate safe for downstream output mapping."""

    answer: Answer
    statements: tuple[AnswerStatement, ...]
    evidence_spans: tuple[EvidenceSpan, ...]
    statement_evidence: tuple[StatementEvidence, ...]
    validations: tuple[AnswerValidation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_terminal_aggregate_shape(self) -> Self:
        """Keep persisted answer content aligned with its terminal lifecycle state."""

        if self.answer.state is AnswerState.STREAMED:
            if not self.statements or not self.evidence_spans or not self.statement_evidence:
                raise ValueError("STREAMED answer result requires statements and evidence")
        elif self.answer.state in {AnswerState.INSUFFICIENT, AnswerState.REJECTED}:
            if self.statements or self.evidence_spans or self.statement_evidence:
                raise ValueError(
                    "non-streamed answer result must not expose statements or evidence"
                )
        else:
            raise ValueError("validated answer result requires a terminal output state")
        if any(validation.answer_id != self.answer.id for validation in self.validations):
            raise ValueError("validation result does not belong to the answer")

        aggregate_workspace_ids = (
            *(statement.workspace_id for statement in self.statements),
            *(span.workspace_id for span in self.evidence_spans),
            *(relationship.workspace_id for relationship in self.statement_evidence),
            *(validation.workspace_id for validation in self.validations),
        )
        if any(
            workspace_id != self.answer.workspace_id for workspace_id in aggregate_workspace_ids
        ):
            raise ValueError("validated answer result crosses a workspace boundary")

        statement_ids = {statement.id for statement in self.statements}
        span_ids = {span.id for span in self.evidence_spans}
        if any(statement.answer_id != self.answer.id for statement in self.statements):
            raise ValueError("statement does not belong to the answer")
        if any(
            relationship.answer_statement_id not in statement_ids
            or relationship.evidence_span_id not in span_ids
            for relationship in self.statement_evidence
        ):
            raise ValueError("statement evidence does not belong to the answer result")
        if any(
            validation.answer_statement_id is not None
            and validation.answer_statement_id not in statement_ids
            for validation in self.validations
        ):
            raise ValueError("statement validation does not belong to the answer result")
        return self
