"""Deterministic bounded context selection for the M1 retrieval slice."""

from collections.abc import Sequence
from itertools import zip_longest
from uuid import UUID

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    M1SelectionProfile,
    M1SelectionResult,
    QualifiedDenseHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin

type CanonicalLaneHit = LexicalSearchHit | QualifiedDenseHit


def select_m1_context(
    *,
    lexical_hits: Sequence[LexicalSearchHit],
    dense_hits: Sequence[QualifiedDenseHit],
    profile: M1SelectionProfile,
) -> M1SelectionResult:
    """Interleave canonical lanes and select unique chunks under both context limits."""

    selected: list[SelectedRetrievalCandidate] = []
    seen_chunk_ids: set[UUID] = set()
    selected_character_count = 0

    for origin_rank, (lexical_hit, dense_hit) in enumerate(
        zip_longest(lexical_hits, dense_hits),
        start=1,
    ):
        lane_hits: tuple[
            tuple[RetrievalOrigin, CanonicalLaneHit | None],
            tuple[RetrievalOrigin, CanonicalLaneHit | None],
        ] = (
            (RetrievalOrigin.LEXICAL, lexical_hit),
            (RetrievalOrigin.DENSE, dense_hit),
        )
        for origin, hit in lane_hits:
            if hit is None:
                continue
            if len(selected) >= profile.context_chunk_limit:
                return M1SelectionResult(profile=profile, candidates=tuple(selected))

            if hit.chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(hit.chunk_id)

            character_count = len(hit.text)
            if selected_character_count + character_count > profile.context_character_limit:
                continue

            selected.append(
                _selected_candidate(
                    hit,
                    origin=origin,
                    rank=origin_rank,
                    selection_rank=len(selected) + 1,
                )
            )
            selected_character_count += character_count

    return M1SelectionResult(profile=profile, candidates=tuple(selected))


def _selected_candidate(
    hit: CanonicalLaneHit,
    *,
    origin: RetrievalOrigin,
    rank: int,
    selection_rank: int,
) -> SelectedRetrievalCandidate:
    """Copy one canonical hit into the provider-neutral selected result."""

    return SelectedRetrievalCandidate(
        chunk_id=hit.chunk_id,
        document_id=hit.document_id,
        document_version_id=hit.document_version_id,
        structure_block_id=hit.structure_block_id,
        source_locator_kind=hit.source_locator_kind,
        page_number=hit.page_number,
        text=hit.text,
        origin=origin,
        rank=rank,
        raw_score=hit.raw_score,
        selection_rank=selection_rank,
    )
