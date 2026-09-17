"""Typed input and result contracts for bounded provider-neutral retrieval."""

from enum import StrEnum
from math import isfinite
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.domain import (
    ConfigurationVersion,
    NormalizedQuery,
    RetrievalFilters,
)

SearchableText = Annotated[str, StringConstraints(min_length=1)]


class EmbeddingPurpose(StrEnum):
    """Semantic role supplied to asymmetric embedding providers."""

    QUERY = "QUERY"
    DOCUMENT = "DOCUMENT"


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
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    text: SearchableText
    raw_score: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only hits without rewriting canonical chunk text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> LexicalSearchHit:
        """Require a physical page only for PDF-backed search hits."""

        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE search hit requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE search hit must not define page_number")
        return self


class EmbeddingInput(ImmutableSearchContract):
    """One correlated text input in a bounded embedding batch."""

    input_id: UUID
    text: SearchableText
    purpose: EmbeddingPurpose

    @field_validator("text")
    @classmethod
    def require_visible_embedding_text(cls, value: str) -> str:
        """Reject whitespace-only text without rewriting the provider input."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value


class EmbeddingRequest(ImmutableSearchContract):
    """One versioned and bounded provider request for ordered text inputs."""

    inputs: tuple[EmbeddingInput, ...] = Field(min_length=1, max_length=100)
    embedding_config_version: ConfigurationVersion

    @field_validator("inputs")
    @classmethod
    def require_unique_embedding_input_ids(
        cls,
        value: tuple[EmbeddingInput, ...],
    ) -> tuple[EmbeddingInput, ...]:
        """Reject ambiguous input identifiers before a provider call."""

        input_ids = [item.input_id for item in value]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("embedding input IDs must be unique")
        return value


class EmbeddingVector(ImmutableSearchContract):
    """One finite provider vector correlated to its original input."""

    input_id: UUID
    values: tuple[float, ...] = Field(min_length=1, max_length=4_096)
    embedding_config_version: ConfigurationVersion

    @field_validator("values")
    @classmethod
    def require_finite_embedding_values(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Reject NaN and infinite values before indexing or retrieval."""

        if any(not isfinite(item) for item in value):
            raise ValueError("embedding values must be finite")
        return value


class DenseRetrievalQuery(ImmutableSearchContract):
    """One bounded vector query with canonical eligibility filters."""

    query_vector: tuple[float, ...] = Field(min_length=1, max_length=4_096)
    filters: RetrievalFilters
    embedding_config_version: ConfigurationVersion
    dense_config_version: ConfigurationVersion
    candidate_limit: int = Field(ge=1, le=100)

    @field_validator("query_vector")
    @classmethod
    def require_finite_query_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Reject malformed query vectors before crossing a provider boundary."""

        if any(not isfinite(item) for item in value):
            raise ValueError("query vector values must be finite")
        return value


class DenseRetrievalHit(ImmutableSearchContract):
    """One provider candidate that still requires canonical eligibility checks."""

    chunk_id: UUID
    raw_score: float = Field(allow_inf_nan=False)


class RerankCandidate(ImmutableSearchContract):
    """One bounded canonical candidate passed to a reranking provider."""

    retrieval_candidate_id: UUID
    text: SearchableText

    @field_validator("text")
    @classmethod
    def require_visible_rerank_text(cls, value: str) -> str:
        """Reject whitespace-only candidate text before a provider call."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value


class RerankRequest(ImmutableSearchContract):
    """One versioned, bounded reranking request over canonical candidates."""

    normalized_query: NormalizedQuery
    candidates: tuple[RerankCandidate, ...] = Field(min_length=1, max_length=100)
    reranker_config_version: ConfigurationVersion

    @field_validator("candidates")
    @classmethod
    def require_unique_rerank_candidate_ids(
        cls,
        value: tuple[RerankCandidate, ...],
    ) -> tuple[RerankCandidate, ...]:
        """Reject ambiguous candidate identifiers before a provider call."""

        candidate_ids = [candidate.retrieval_candidate_id for candidate in value]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("rerank candidate IDs must be unique")
        return value


class RerankResult(ImmutableSearchContract):
    """One finite reranker score with an explicit stable rank."""

    retrieval_candidate_id: UUID
    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)
