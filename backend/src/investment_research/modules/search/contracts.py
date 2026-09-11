"""Typed input and result contracts for bounded lexical retrieval."""

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from investment_research.modules.search.domain import (
    ConfigurationVersion,
    NormalizedQuery,
    RetrievalFilters,
)

SearchableText = Annotated[str, StringConstraints(min_length=1)]


class ImmutableSearchContract(BaseModel):
    """Reject unknown fields and keep application boundary values immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LexicalSearchProfile(ImmutableSearchContract):
    """Versioned and bounded configuration selected for lexical retrieval."""

    lexical_config_version: ConfigurationVersion
    candidate_limit: int = Field(ge=1, le=100)


class LexicalSearchQuery(ImmutableSearchContract):
    """One normalized, reproducible, and bounded PostgreSQL search request."""

    normalized_query: NormalizedQuery
    filters: RetrievalFilters
    lexical_config_version: ConfigurationVersion
    candidate_limit: int = Field(ge=1, le=100)


class LexicalSearchHit(ImmutableSearchContract):
    """One eligible canonical chunk selected by lexical ranking."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    page_number: int = Field(ge=1)
    text: SearchableText
    raw_score: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only hits without rewriting canonical chunk text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value
