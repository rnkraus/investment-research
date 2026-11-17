"""Rank complete sources and fit page evidence into the RRF context budget."""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from math import log
from uuid import UUID

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.observation_dates import requested_observation_date

PAGE_SELECTOR_VERSION = "m1-5-rrf-k60-page-context-v2"
PAGE_SELECTOR_VERSIONS = frozenset({"m1-5-rrf-k60-page-context-v1", PAGE_SELECTOR_VERSION})
PAGE_CONTEXT_SLOTS = 4
STRUCTURED_CONTEXT_LIMIT = 7
_OBSERVATION_METRIC = re.compile(
    r"^\s*(?:what(?:\s+exact)?|how much)\s+(?P<metric>.+?)\s+"
    r"(?:did|does|was|were|is|has|had)\b",
    re.IGNORECASE,
)


def observation_metric(question: str) -> str | None:
    """Recognize a named metric in the leading observation clause."""
    if requested_observation_date(question) is None:
        return None
    match = _OBSERVATION_METRIC.match(question)
    if match is None or not 2 <= len(match["metric"].split()) <= 8:
        return None
    return match["metric"]


def term_weights(
    query_terms: frozenset[str], sources: Iterable[frozenset[str]]
) -> dict[str, float]:
    count = 0
    frequency: Counter[str] = Counter()
    for words in sources:
        count += 1
        frequency.update(words & query_terms)
    return {
        term: log(1 + (count - matches + 0.5) / (matches + 0.5))
        for term, matches in frequency.items()
    }


def weighted_overlap(words: frozenset[str], weights: dict[str, float]) -> float:
    """Score rare matching terms with a mild length penalty."""
    return float(
        sum(weights[term] for term in sorted(words & weights.keys()))
        / (1 + len(words) / 150) ** 0.35
    )


def add_page_context(
    selected: tuple[SelectedRetrievalCandidate, ...],
    hits: Sequence[LexicalSearchHit],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[SelectedRetrievalCandidate, ...]:
    """Reserve the first half of the context for RRF and deduplicate page additions."""
    split = (chunk_limit + 1) // 2
    return _fit(
        (*selected[:split], *map(_structured, hits), *selected[split:]),
        chunk_limit,
        character_limit,
    )


def add_observation_context(
    selected: tuple[SelectedRetrievalCandidate, ...],
    hit: LexicalSearchHit,
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[SelectedRetrievalCandidate, ...]:
    """Keep earlier evidence and insert at most one additional observation passage."""
    if any(item.chunk_id == hit.chunk_id for item in selected):
        return selected
    for prefix_length in (min(len(selected), chunk_limit - 1), len(selected) - 1):
        if prefix_length >= 0:
            prefix = selected[:prefix_length]
            if sum(len(item.text) for item in prefix) + len(hit.text) <= character_limit:
                return _fit((*prefix, _structured(hit)), chunk_limit, character_limit)
    return selected


def _structured(hit: LexicalSearchHit) -> SelectedRetrievalCandidate:
    return SelectedRetrievalCandidate(
        **hit.model_dump(), origin=RetrievalOrigin.STRUCTURED, rank=1, selection_rank=1
    )


def _fit(
    candidates: Iterable[SelectedRetrievalCandidate], chunk_limit: int, character_limit: int
) -> tuple[SelectedRetrievalCandidate, ...]:
    seen: set[UUID] = set()
    result: list[SelectedRetrievalCandidate] = []
    used_characters = 0
    for item in candidates:
        if len(result) == chunk_limit:
            break
        if item.chunk_id in seen or used_characters + len(item.text) > character_limit:
            continue
        seen.add(item.chunk_id)
        used_characters += len(item.text)
        result.append(item.model_copy(update={"selection_rank": len(result) + 1}))
    return tuple(result)
