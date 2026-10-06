"""Pure bounded RRF over already-qualified canonical lexical and dense lanes."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    LexicalSearchHit,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.fusion_contracts import (
    RrfFusedCandidate,
    RrfLaneContribution,
    RrfOrigin,
    RrfSelectedCandidate,
    RrfSelectionProfile,
    RrfSelectionResult,
    reciprocal_rank_score,
)

type CanonicalHit = LexicalSearchHit | QualifiedDenseHit


@dataclass(slots=True)
class _FusionEntry:
    hit: CanonicalHit
    contributions: list[RrfLaneContribution] = field(default_factory=list)


def fuse_rrf_hits(
    *,
    lexical_hits: Sequence[LexicalSearchHit],
    dense_hits: Sequence[QualifiedDenseHit],
) -> tuple[RrfFusedCandidate, ...]:
    """Rank unique chunks by equal-weight RRF, then ascending chunk UUID for ties.

    The caller must apply canonical workspace and eligibility checks first. Raw provider
    hits are not accepted here. Rank means position in each qualified input sequence.
    """

    entries: dict[UUID, _FusionEntry] = {}
    lanes: tuple[tuple[RrfOrigin, Sequence[CanonicalHit]], ...] = (
        (RetrievalOrigin.LEXICAL, lexical_hits),
        (RetrievalOrigin.DENSE, dense_hits),
    )
    for origin, hits in lanes:
        if len(hits) > M1_LANE_CANDIDATE_LIMIT:
            raise ValueError("RRF lane exceeds the candidate limit")
        seen: set[UUID] = set()
        for rank, hit in enumerate(hits, start=1):
            expected_type = (
                LexicalSearchHit if origin is RetrievalOrigin.LEXICAL else QualifiedDenseHit
            )
            if not isinstance(hit, expected_type):
                raise TypeError("RRF requires canonical hits of the declared lane")
            if hit.chunk_id in seen:
                raise ValueError("RRF lane contains duplicate chunks")
            seen.add(hit.chunk_id)
            entry = entries.get(hit.chunk_id)
            if entry is None:
                entry = _FusionEntry(hit=hit)
                entries[hit.chunk_id] = entry
            elif entry.hit.model_dump(exclude={"raw_score"}) != hit.model_dump(
                exclude={"raw_score"}
            ):
                raise ValueError("RRF lanes disagree about canonical chunk content")
            # A shared chunk keeps lexical as its representative, but both lane votes survive.
            entry.contributions.append(
                RrfLaneContribution(origin=origin, rank=rank, raw_score=hit.raw_score)
            )

    ranked = sorted(
        entries.values(),
        key=lambda entry: (
            -reciprocal_rank_score(tuple(entry.contributions)),
            entry.hit.chunk_id.int,
        ),
    )
    return tuple(
        RrfFusedCandidate(
            hit=entry.hit,
            contributions=tuple(entry.contributions),
            fused_rank=rank,
            fused_score=float(reciprocal_rank_score(tuple(entry.contributions))),
        )
        for rank, entry in enumerate(ranked, start=1)
    )


def select_rrf_context(
    *,
    lexical_hits: Sequence[LexicalSearchHit],
    dense_hits: Sequence[QualifiedDenseHit],
    profile: RrfSelectionProfile,
) -> RrfSelectionResult:
    """Keep whole chunks in fused order under the existing M1 context limits."""

    ranked = fuse_rrf_hits(lexical_hits=lexical_hits, dense_hits=dense_hits)
    selected: list[RrfSelectedCandidate] = []
    character_count = 0
    for candidate in ranked:
        if len(selected) >= profile.context_chunk_limit:
            break
        size = len(candidate.hit.text)
        if character_count + size > profile.context_character_limit:
            continue
        selected.append(RrfSelectedCandidate(candidate=candidate, selection_rank=len(selected) + 1))
        character_count += size
    return RrfSelectionResult(profile=profile, candidates=tuple(selected))
