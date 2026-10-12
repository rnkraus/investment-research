"""Domain values and invariants for reproducible retrieval."""

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

from investment_research.modules.search.limits import MAX_CONTEXT_CHUNKS

QueryText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
NormalizedQuery = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
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

LEXICAL_ORDER_SELECTOR_CONFIG_VERSION = "m0.0-lexical-order-v1"


class RetrievalOrigin(StrEnum):
    """Candidate-producing routes in the target retrieval pipeline."""

    LEXICAL = "LEXICAL"
    DENSE = "DENSE"
    STRUCTURED = "STRUCTURED"


class RetrievalMode(StrEnum):
    """Versioned retrieval lane combinations supported by persisted runs."""

    LEXICAL = "LEXICAL"
    DUAL_LANE = "DUAL_LANE"


class SearchScopeError(ValueError):
    """Raised when search data crosses the trusted workspace boundary."""


class SearchRelationshipError(ValueError):
    """Raised when retrieval records do not describe one coherent run."""


class ImmutableSearchModel(BaseModel):
    """Shared validation policy for immutable search snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RetrievalFilters(ImmutableSearchModel):
    """Typed snapshot of the explicit filters applied to a retrieval run."""

    document_ids: tuple[UUID, ...] = ()
    available_at_cutoff: AwareDatetime | None = None
    captured_at_cutoff: AwareDatetime | None = None

    @field_validator("document_ids")
    @classmethod
    def require_unique_document_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        """Reject ambiguous duplicate filter values while retaining caller order."""

        if len(value) != len(set(value)):
            raise ValueError("document_ids must be unique")
        return value

    @field_validator("available_at_cutoff", "captured_at_cutoff")
    @classmethod
    def normalize_temporal_cutoffs(cls, value: datetime | None) -> datetime | None:
        """Normalize optional temporal-eligibility cutoffs to UTC."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_public_cutoff_for_archive_cutoff(self) -> Self:
        """Require archive-time filtering to retain public-availability eligibility."""

        if self.captured_at_cutoff is not None and self.available_at_cutoff is None:
            raise ValueError("captured_at_cutoff requires available_at_cutoff")
        return self


class RetrievalRun(ImmutableSearchModel):
    """One completed, reproducible execution of the retrieval pipeline."""

    id: UUID
    workspace_id: UUID
    research_request_id: UUID
    query_text: QueryText
    normalized_query: NormalizedQuery
    filters: RetrievalFilters
    mode: RetrievalMode
    lexical_config_version: ConfigurationVersion
    embedding_config_version: ConfigurationVersion | None = None
    dense_index_config_version: ConfigurationVersion | None = None
    selector_config_version: ConfigurationVersion
    lexical_candidate_limit: int = Field(ge=1, le=100)
    dense_candidate_limit: int = Field(default=0, ge=0, le=100)
    context_chunk_limit: int | None = Field(default=None, ge=1, le=MAX_CONTEXT_CHUNKS)
    context_character_limit: int | None = Field(default=None, ge=1, le=24_000)
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @field_validator("query_text")
    @classmethod
    def require_visible_query(cls, value: str) -> str:
        """Reject whitespace-only input without altering the user's exact question."""

        if not value.strip():
            raise ValueError("query_text must contain a non-whitespace character")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_run_times(cls, value: datetime) -> datetime:
        """Normalize retrieval timing to UTC."""

        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_ordered_run_times(self) -> Self:
        """Reject incoherent timing and lane-specific configuration snapshots."""

        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.mode is RetrievalMode.LEXICAL:
            if (
                self.embedding_config_version is not None
                or self.dense_index_config_version is not None
                or self.dense_candidate_limit != 0
                or self.context_chunk_limit is not None
                or self.context_character_limit is not None
            ):
                raise ValueError("LEXICAL retrieval must not define dense or context settings")
        elif (
            self.embedding_config_version is None
            or self.dense_index_config_version is None
            or self.dense_candidate_limit == 0
            or self.context_chunk_limit is None
            or self.context_character_limit is None
        ):
            raise ValueError("DUAL_LANE retrieval requires dense and context settings")
        return self


class RetrievalCandidate(ImmutableSearchModel):
    """One ranked chunk selected by a reproducible retrieval run."""

    id: UUID
    workspace_id: UUID
    retrieval_run_id: UUID
    chunk_id: UUID
    origin: RetrievalOrigin
    rank: int = Field(ge=1)
    raw_score: float = Field(allow_inf_nan=False)
    selection_rank: int | None = Field(default=None, ge=1)
    fused_rank: int | None = Field(default=None, ge=1)
    reranker_score: float | None = Field(default=None, allow_inf_nan=False)
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize candidate creation time to UTC."""

        return value.astimezone(UTC)
