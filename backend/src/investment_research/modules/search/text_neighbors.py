"""Select a bounded pair of canonical neighbors for explanatory questions."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.observation_dates import (
    contains_observation_date,
    requested_observation_date,
)

TEXT_NEIGHBOR_SELECTOR_VERSION = "m1-5-rrf-k60-text-neighbors-v1"
TEXT_NEIGHBOR_CONTEXT_LIMIT = 8
TEXT_NEIGHBOR_ANCHOR_LIMIT = 4
TEXT_NEIGHBOR_RADIUS = 2
_CAUSAL_QUERY = re.compile(
    r"\bwhy\b|\bhow\b.*\b(?:help|caus\w*|lead to|affect\w*|reduc\w*|"
    r"increas\w*|prevent\w*)\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class TextNeighbor:
    """One eligible neighbor and the lexical terms needed to compare its window."""

    anchor_id: UUID
    offset: int
    hit: LexicalSearchHit
    anchor_terms: frozenset[str]
    neighbor_terms: frozenset[str]
    query_terms: frozenset[str]


def seeks_explanation(question: str) -> bool:
    return _CAUSAL_QUERY.search(question) is not None


def _matching_terms(words: frozenset[str], query_terms: frozenset[str]) -> frozenset[str]:
    return (words & query_terms) | frozenset(
        word[:-1] for word in words if word.endswith("s") and word[:-1] in query_terms
    )


def select_text_neighbors(
    question: str,
    selected: tuple[SelectedRetrievalCandidate, ...],
    neighbors: Sequence[TextNeighbor],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[tuple[SelectedRetrievalCandidate, ...], tuple[LexicalSearchHit, ...]]:
    """Insert one relevant pair in document order while preserving the RRF anchors."""
    selected_ids = {item.chunk_id for item in selected}
    offsets = {(item.anchor_id, item.offset): item for item in neighbors}
    date = requested_observation_date(question)
    choices: list[tuple[int, int, int, int, TextNeighbor, TextNeighbor]] = []
    for index, anchor in enumerate(selected[:TEXT_NEIGHBOR_ANCHOR_LIMIT]):
        for direction in (-TEXT_NEIGHBOR_RADIUS, 1):
            first = offsets.get((anchor.chunk_id, direction))
            second = offsets.get((anchor.chunk_id, direction + 1))
            if first is None or second is None:
                continue
            if (
                first.hit.chunk_id == second.hit.chunk_id
                or first.hit.structure_block_id == second.hit.structure_block_id
                or first.hit.structure_block_id == anchor.structure_block_id
                or second.hit.structure_block_id == anchor.structure_block_id
                or first.hit.chunk_id in selected_ids
                or second.hit.chunk_id in selected_ids
            ):
                continue
            if date is not None and not all(
                contains_observation_date(item.hit.text, date) for item in (first, second)
            ):
                continue
            anchor_terms = _matching_terms(first.anchor_terms, first.query_terms)
            first_terms = _matching_terms(first.neighbor_terms, first.query_terms)
            second_terms = _matching_terms(second.neighbor_terms, second.query_terms)
            shared = (first_terms & second_terms) - anchor_terms
            if len(shared) >= 2:
                choices.append(
                    (
                        -len(shared),
                        -len(first_terms | second_terms),
                        index,
                        direction,
                        first,
                        second,
                    )
                )

    for score, _, index, _, first, second in sorted(choices, key=lambda item: item[:4]):
        anchor = selected[index]
        # Protect the top retrieval anchors and prefer replacing lower-ranked unrelated text.
        removable = sorted(
            selected[TEXT_NEIGHBOR_ANCHOR_LIMIT:],
            key=lambda item: (
                item.document_version_id == anchor.document_version_id,
                -item.selection_rank,
            ),
        )
        pair = tuple(
            item.hit.model_copy(update={"raw_score": float(-score)}) for item in (first, second)
        )
        remove_count = max(0, len(selected) + len(pair) - chunk_limit)
        if remove_count > len(removable):
            continue
        removed = {item.chunk_id for item in removable[:remove_count]}
        kept = [item for item in selected if item.chunk_id not in removed]
        context_size = sum(len(item.text) for item in kept) + sum(len(item.text) for item in pair)
        if context_size > character_limit:
            continue
        additions = [
            SelectedRetrievalCandidate(
                **hit.model_dump(), origin=RetrievalOrigin.STRUCTURED, rank=rank, selection_rank=1
            )
            for rank, hit in enumerate(pair, 1)
        ]
        anchor_index = kept.index(anchor) + 1
        kept[anchor_index:anchor_index] = additions
        return (
            tuple(
                item.model_copy(update={"selection_rank": rank})
                for rank, item in enumerate(kept, 1)
            ),
            pair,
        )
    return selected, ()
