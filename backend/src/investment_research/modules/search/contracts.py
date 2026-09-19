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
    RetrievalOrigin,
)

SearchableText = Annotated[str, StringConstraints(min_length=1)]
DenseVectorId = Annotated[
    str,
    StringConstraints(pattern=r"^v1:[a-f0-9]{64}$"),
]

M1_EMBEDDING_BATCH_LIMIT = 16
M1_EMBEDDING_CHARACTER_LIMIT = 32_000
M1_EMBEDDING_DIMENSIONS = 1_536
M1_SELECTOR_CONFIG_VERSION = "m1-lexical-first-round-robin-v1"
M1_CONTEXT_CHUNK_LIMIT = 8
M1_CONTEXT_CHARACTER_LIMIT = 24_000


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

    inputs: tuple[EmbeddingInput, ...] = Field(
        min_length=1,
        max_length=M1_EMBEDDING_BATCH_LIMIT,
    )
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
        if sum(len(item.text) for item in value) > M1_EMBEDDING_CHARACTER_LIMIT:
            raise ValueError("embedding request exceeds the character limit")
        return value


class EmbeddingVector(ImmutableSearchContract):
    """One finite provider vector correlated to its original input."""

    input_id: UUID
    values: tuple[float, ...] = Field(
        min_length=M1_EMBEDDING_DIMENSIONS,
        max_length=M1_EMBEDDING_DIMENSIONS,
    )
    embedding_config_version: ConfigurationVersion

    @field_validator("values")
    @classmethod
    def require_finite_embedding_values(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Reject NaN and infinite values before indexing or retrieval."""

        if any(not isfinite(item) for item in value):
            raise ValueError("embedding values must be finite")
        return value


class DenseIndexVector(ImmutableSearchContract):
    """One application-owned vector and its bounded provider metadata."""

    vector_id: DenseVectorId
    values: tuple[float, ...] = Field(
        min_length=M1_EMBEDDING_DIMENSIONS,
        max_length=M1_EMBEDDING_DIMENSIONS,
    )
    chunk_id: UUID
    document_version_id: UUID
    chunk_set_id: UUID
    embedding_config_version: ConfigurationVersion
    dense_index_config_version: ConfigurationVersion

    @field_validator("values")
    @classmethod
    def require_finite_index_values(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        """Reject malformed vectors before crossing the dense-index boundary."""

        if any(not isfinite(item) for item in value):
            raise ValueError("dense index values must be finite")
        return value


class DenseIndexWriteRequest(ImmutableSearchContract):
    """One bounded idempotent write of correlated application vectors."""

    vectors: tuple[DenseIndexVector, ...] = Field(
        min_length=1,
        max_length=M1_EMBEDDING_BATCH_LIMIT,
    )
    dense_index_config_version: ConfigurationVersion

    @model_validator(mode="after")
    def require_coherent_vectors(self) -> DenseIndexWriteRequest:
        """Reject duplicate identities or mixed dense-index configurations."""

        vector_ids = [vector.vector_id for vector in self.vectors]
        chunk_ids = [vector.chunk_id for vector in self.vectors]
        if len(vector_ids) != len(set(vector_ids)):
            raise ValueError("dense index vector IDs must be unique")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("dense index chunk IDs must be unique")
        if any(
            vector.dense_index_config_version != self.dense_index_config_version
            for vector in self.vectors
        ):
            raise ValueError("dense index configuration versions must match")
        return self


class DenseIndexWriteResult(ImmutableSearchContract):
    """The deterministic vector identities acknowledged by one successful write."""

    vector_ids: tuple[DenseVectorId, ...] = Field(
        min_length=1,
        max_length=M1_EMBEDDING_BATCH_LIMIT,
    )

    @field_validator("vector_ids")
    @classmethod
    def require_unique_vector_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject an ambiguous provider acknowledgement."""

        if len(value) != len(set(value)):
            raise ValueError("dense index result vector IDs must be unique")
        return value


class DenseRetrievalQuery(ImmutableSearchContract):
    """One bounded vector query with canonical eligibility filters."""

    query_vector: tuple[float, ...] = Field(
        min_length=M1_EMBEDDING_DIMENSIONS,
        max_length=M1_EMBEDDING_DIMENSIONS,
    )
    filters: RetrievalFilters
    embedding_config_version: ConfigurationVersion
    dense_index_config_version: ConfigurationVersion
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


class DenseQualificationRequest(ImmutableSearchContract):
    """Provider hits plus the canonical filters and versions they must still satisfy."""

    provider_hits: tuple[DenseRetrievalHit, ...] = Field(max_length=100)
    filters: RetrievalFilters
    lexical_config_version: ConfigurationVersion
    embedding_config_version: ConfigurationVersion
    dense_index_config_version: ConfigurationVersion

    @field_validator("provider_hits")
    @classmethod
    def require_unique_provider_chunk_ids(
        cls,
        value: tuple[DenseRetrievalHit, ...],
    ) -> tuple[DenseRetrievalHit, ...]:
        """Reject an ambiguous provider ranking before canonical reads."""

        chunk_ids = [hit.chunk_id for hit in value]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("dense provider chunk IDs must be unique")
        return value


class QualifiedDenseHit(ImmutableSearchContract):
    """One provider hit rehydrated from eligible canonical PostgreSQL state."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    text: SearchableText
    raw_score: float = Field(allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only canonical chunks without rewriting their text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> QualifiedDenseHit:
        """Require a physical page only for PDF-backed qualified hits."""

        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE qualified dense hit requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE qualified dense hit must not define page_number")
        return self


class M1SelectionProfile(ImmutableSearchContract):
    """Versioned limits for deterministic lexical-first context selection."""

    selector_config_version: ConfigurationVersion = M1_SELECTOR_CONFIG_VERSION
    context_chunk_limit: int = Field(default=M1_CONTEXT_CHUNK_LIMIT, ge=1, le=8)
    context_character_limit: int = Field(
        default=M1_CONTEXT_CHARACTER_LIMIT,
        ge=1,
        le=24_000,
    )

    @field_validator("selector_config_version")
    @classmethod
    def require_supported_selector_version(cls, value: str) -> str:
        """Prevent a configuration label from silently changing selector behavior."""

        if value != M1_SELECTOR_CONFIG_VERSION:
            raise ValueError("unsupported M1 selector configuration version")
        return value


class SelectedRetrievalCandidate(ImmutableSearchContract):
    """One canonical route candidate selected into final M1 context order."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    structure_block_id: UUID
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    text: SearchableText
    origin: RetrievalOrigin
    rank: int = Field(ge=1)
    raw_score: float = Field(allow_inf_nan=False)
    selection_rank: int = Field(ge=1, le=M1_CONTEXT_CHUNK_LIMIT)

    @field_validator("text")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        """Reject whitespace-only canonical chunks without rewriting their text."""

        if not value.strip():
            raise ValueError("text must contain a non-whitespace character")
        return value

    @field_validator("origin")
    @classmethod
    def require_m1_lane(cls, value: RetrievalOrigin) -> RetrievalOrigin:
        """Keep the M1 selector limited to its lexical and dense lanes."""

        if value not in {RetrievalOrigin.LEXICAL, RetrievalOrigin.DENSE}:
            raise ValueError("M1 selection supports only lexical and dense candidates")
        return value

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> SelectedRetrievalCandidate:
        """Require a physical page only for PDF-backed selected candidates."""

        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE selected candidate requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE selected candidate must not define page_number")
        return self


class M1SelectionResult(ImmutableSearchContract):
    """Bounded final context plus the exact selector profile that produced it."""

    profile: M1SelectionProfile
    candidates: tuple[SelectedRetrievalCandidate, ...] = Field(max_length=M1_CONTEXT_CHUNK_LIMIT)

    @model_validator(mode="after")
    def require_coherent_selection(self) -> M1SelectionResult:
        """Require unique chunks, contiguous order, and the configured context bounds."""

        chunk_ids = [candidate.chunk_id for candidate in self.candidates]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("selected chunk IDs must be unique")
        if [candidate.selection_rank for candidate in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("selection ranks must be contiguous and ordered")
        if len(self.candidates) > self.profile.context_chunk_limit:
            raise ValueError("selection exceeds the configured chunk limit")
        if sum(len(candidate.text) for candidate in self.candidates) > (
            self.profile.context_character_limit
        ):
            raise ValueError("selection exceeds the configured character limit")
        return self


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
