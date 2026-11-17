"""Select bounded canonical text neighbors with new question-relevant information."""

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

TEXT_NEIGHBOR_SELECTOR_VERSION = "m1-5-rrf-k60-text-neighbors-v2"
TEXT_NEIGHBOR_SELECTOR_VERSIONS = frozenset(
    {
        "m1-5-rrf-k60-text-neighbors-v1",
        TEXT_NEIGHBOR_SELECTOR_VERSION,
    }
)
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


def _select_explanatory_pair(
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


_EXPLANATION = re.compile(r"\b(?:how|why|cause|mechanism|bottleneck|reason|effect)\b", re.I)
_EXPLANATION_CUE = re.compile(
    r"\b(?:because|which (?:causes?|means|allows?|reduces?)|thereby|enabl\w*|"
    r"leads? to|leaving|as a result|so that|by (?:reduc|prevent|avoid|increas|shorten)\w*)\b",
    re.I,
)
_QUANTITY = re.compile(
    r"\b(?:how (?:much|many|long)|amount|value|price|rate|capacity|cost|"
    r"revenue|arr|budget|spread)\b",
    re.I,
)
_QUANTITY_CUE = re.compile(
    r"(?:[$€£]\s*\d|\b\d[\d.,]*\s*(?:%|bps\b|MW\b|GW\b|days?\b|months?\b|years?\b|"
    r"million\b|billion\b|percentage points?\b))",
    re.I,
)
_LOCATIONS = re.compile(
    r"\b(?:where|location\w*|cit(?:y|ies)|countr(?:y|ies)|campus\w*|sites?)\b", re.I
)
_LOCATION_CUE = re.compile(r"\b(?:located|based|situated)\s+(?:in|at|near|approximately)\b", re.I)
_CONTINUATION = re.compile(
    r"^(?:these|this|that|it|they|such|the (?:site|project|deal|technology))\b", re.I
)


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _select_informative_neighbor(
    question: str,
    selected: tuple[SelectedRetrievalCandidate, ...],
    neighbors: Sequence[TextNeighbor],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[tuple[SelectedRetrievalCandidate, ...], tuple[LexicalSearchHit, ...]]:
    selected_ids = {item.chunk_id for item in selected}
    block_ids = {item.structure_block_id for item in selected}
    texts = {_normalized(item.text) for item in selected}
    anchors = {item.chunk_id: item for item in selected[:TEXT_NEIGHBOR_ANCHOR_LIMIT]}
    selected_terms = frozenset(term for item in neighbors for term in item.anchor_terms)
    selected_terms |= frozenset(
        term
        for item in neighbors
        if item.hit.chunk_id in selected_ids
        for term in item.neighbor_terms
    )
    date = requested_observation_date(question)
    signals = tuple(
        cue
        for intent, cue in (
            (_EXPLANATION, _EXPLANATION_CUE),
            (_QUANTITY, _QUANTITY_CUE),
            (_LOCATIONS, _LOCATION_CUE),
        )
        if intent.search(question)
    )
    choices = []
    for position, item in enumerate(neighbors):
        candidate = item.hit
        if (
            candidate.chunk_id in selected_ids
            or candidate.structure_block_id in block_ids
            or _normalized(candidate.text) in texts
            or (date is not None and not contains_observation_date(candidate.text, date))
        ):
            continue
        anchor = anchors[item.anchor_id]
        matched = _matching_terms(item.neighbor_terms, item.query_terms)
        new = matched - _matching_terms(selected_terms, item.query_terms)
        fact_signal = any(
            cue.search(candidate.text) and not cue.search(anchor.text) for cue in signals
        )
        connected = (
            abs(item.offset) == 1 and _CONTINUATION.match(candidate.text.strip()) is not None
        )
        if not matched or (
            len(matched) < 2 and not connected and len(item.neighbor_terms & item.anchor_terms) < 2
        ):
            continue
        if len(new) < 2 and not fact_signal:
            continue
        choices.append(
            (
                -len(new),
                -int(fact_signal),
                -len(matched),
                abs(item.offset),
                anchor.selection_rank,
                candidate.chunk_id,
                position,
            )
        )

    for *_, position in sorted(choices):
        endpoint = neighbors[position]
        anchor = anchors[endpoint.anchor_id]
        additions: tuple[TextNeighbor, ...] = (endpoint,)
        if abs(endpoint.offset) == TEXT_NEIGHBOR_RADIUS:
            middle = next(
                (
                    item
                    for item in neighbors
                    if item.anchor_id == endpoint.anchor_id and item.offset == endpoint.offset // 2
                ),
                None,
            )
            if middle is not None and (
                middle.hit.chunk_id not in selected_ids
                and middle.hit.structure_block_id not in block_ids
                and _normalized(middle.hit.text) not in texts
                and len((middle.neighbor_terms & endpoint.neighbor_terms) - endpoint.anchor_terms)
                >= 2
                and (date is None or contains_observation_date(middle.hit.text, date))
            ):
                additions = tuple(sorted((endpoint, middle), key=lambda item: item.offset))
        # Keep the four leading retrieval anchors. Replace only the trailing selections.
        removable = selected[TEXT_NEIGHBOR_ANCHOR_LIMIT:]
        count = max(0, len(selected) + len(additions) - chunk_limit)
        if count > len(removable):
            continue
        removed = {item.chunk_id for item in removable[-count:]} if count else set()
        kept = [item for item in selected if item.chunk_id not in removed]
        hits = tuple(item.hit for item in additions)
        if (
            sum(len(item.text) for item in kept) + sum(len(hit.text) for hit in hits)
            > character_limit
        ):
            continue
        index = kept.index(anchor) + (endpoint.offset > 0)
        kept[index:index] = [
            SelectedRetrievalCandidate(
                **hit.model_dump(), origin=RetrievalOrigin.STRUCTURED, rank=rank, selection_rank=1
            )
            for rank, hit in enumerate(hits, 1)
        ]
        return (
            tuple(
                item.model_copy(update={"selection_rank": rank})
                for rank, item in enumerate(kept, 1)
            ),
            hits,
        )
    return selected, ()


def select_text_neighbors(
    question: str,
    selected: tuple[SelectedRetrievalCandidate, ...],
    neighbors: Sequence[TextNeighbor],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[tuple[SelectedRetrievalCandidate, ...], tuple[LexicalSearchHit, ...]]:
    """Prefer an explanatory pair, then up to two informative contiguous neighbors."""
    anchors = {item.chunk_id: item for item in selected[:TEXT_NEIGHBOR_ANCHOR_LIMIT]}
    eligible = tuple(
        item
        for item in neighbors
        if item.anchor_id in anchors
        and 0 < abs(item.offset) <= TEXT_NEIGHBOR_RADIUS
        and item.hit.document_id == anchors[item.anchor_id].document_id
        and item.hit.document_version_id == anchors[item.anchor_id].document_version_id
        and item.hit.source_locator_kind == anchors[item.anchor_id].source_locator_kind
    )
    bounds = {
        "chunk_limit": min(chunk_limit, TEXT_NEIGHBOR_CONTEXT_LIMIT),
        "character_limit": character_limit,
    }
    if seeks_explanation(question):
        expanded, added = _select_explanatory_pair(question, selected, eligible, **bounds)
        if added:
            return expanded, added
    return _select_informative_neighbor(question, selected, eligible, **bounds)
