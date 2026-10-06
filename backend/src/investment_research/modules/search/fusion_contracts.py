"""Versioned values for the opt-in M1.5 reciprocal-rank fusion experiment."""

from fractions import Fraction
from typing import Final, Literal

from pydantic import Field, model_validator

from investment_research.modules.search.contracts import (
    M1_CONTEXT_CHARACTER_LIMIT,
    M1_CONTEXT_CHUNK_LIMIT,
    M1_LANE_CANDIDATE_LIMIT,
    ImmutableSearchContract,
    LexicalSearchHit,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalOrigin

RRF_CONFIG_VERSION: Final = "m1-5-rrf-k60-v1"
RRF_RANK_CONSTANT: Final = 60
type RrfOrigin = Literal[RetrievalOrigin.LEXICAL, RetrievalOrigin.DENSE]


class RrfSelectionProfile(ImmutableSearchContract):
    """Keep equal lane weights, k, and the existing M1 context bounds fixed."""

    selector_config_version: Literal["m1-5-rrf-k60-v1"] = RRF_CONFIG_VERSION
    rank_constant: Literal[60] = RRF_RANK_CONSTANT
    context_chunk_limit: int = Field(default=M1_CONTEXT_CHUNK_LIMIT, ge=1, le=8, strict=True)
    context_character_limit: int = Field(
        default=M1_CONTEXT_CHARACTER_LIMIT, ge=1, le=24_000, strict=True
    )


class RrfLaneContribution(ImmutableSearchContract):
    """Preserve one lane's rank and raw score without comparing score scales."""

    origin: RrfOrigin
    rank: int = Field(ge=1, le=M1_LANE_CANDIDATE_LIMIT, strict=True)
    raw_score: float = Field(allow_inf_nan=False)


def reciprocal_rank_score(contributions: tuple[RrfLaneContribution, ...]) -> Fraction:
    """Use exact rational arithmetic so equal sums have an exact tie."""

    return sum(
        (Fraction(1, RRF_RANK_CONSTANT + item.rank) for item in contributions),
        Fraction(0),
    )


class RrfFusedCandidate(ImmutableSearchContract):
    """One canonical chunk with both lane records and its pre-budget fusion rank."""

    hit: LexicalSearchHit | QualifiedDenseHit
    contributions: tuple[RrfLaneContribution, ...] = Field(min_length=1, max_length=2)
    fused_rank: int = Field(ge=1, le=2 * M1_LANE_CANDIDATE_LIMIT, strict=True)
    fused_score: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_coherent_contributions(self) -> RrfFusedCandidate:
        """Reject duplicate lane votes or a score inconsistent with the fixed formula."""

        origins = [item.origin for item in self.contributions]
        if len(origins) != len(set(origins)):
            raise ValueError("RRF contributions must have unique origins")
        if self.fused_score != float(reciprocal_rank_score(self.contributions)):
            raise ValueError("RRF score does not match its lane ranks")
        if self.hit.raw_score != self.contributions[0].raw_score:
            raise ValueError("representative hit must retain its lane raw score")
        return self


class RrfSelectedCandidate(ImmutableSearchContract):
    """Separate context position from the original pre-budget fusion position."""

    candidate: RrfFusedCandidate
    selection_rank: int = Field(ge=1, le=M1_CONTEXT_CHUNK_LIMIT, strict=True)


class RrfSelectionResult(ImmutableSearchContract):
    """Bounded context output; this does not authorize any candidate as evidence."""

    profile: RrfSelectionProfile
    candidates: tuple[RrfSelectedCandidate, ...] = Field(max_length=M1_CONTEXT_CHUNK_LIMIT)

    @model_validator(mode="after")
    def require_coherent_selection(self) -> RrfSelectionResult:
        """Require unique chunks, stable rank order, and both context limits."""

        chunk_ids = [item.candidate.hit.chunk_id for item in self.candidates]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("RRF selected chunks must be unique")
        if [item.selection_rank for item in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise ValueError("RRF selection ranks must be contiguous and ordered")
        fused_ranks = [item.candidate.fused_rank for item in self.candidates]
        if fused_ranks != sorted(set(fused_ranks)):
            raise ValueError("RRF selected fusion ranks must be unique and ordered")
        if len(self.candidates) > self.profile.context_chunk_limit:
            raise ValueError("RRF selection exceeds the configured chunk limit")
        if sum(len(item.candidate.hit.text) for item in self.candidates) > (
            self.profile.context_character_limit
        ):
            raise ValueError("RRF selection exceeds the configured character limit")
        return self
