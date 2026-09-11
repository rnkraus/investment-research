"""Typed application contracts for research retrieval orchestration."""

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from investment_research.modules.research.domain import ResearchRequest, TemporalMode
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
