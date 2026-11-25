"""Rank complete sources and fit page evidence into the RRF context budget."""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import log
from uuid import UUID

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.observation_dates import (
    contains_observation_date,
    requested_observation_date,
)

PAGE_SELECTOR_VERSION = "m1-5-rrf-k60-page-context-v4"
PAGE_SELECTOR_VERSIONS = frozenset(
    {
        "m1-5-rrf-k60-page-context-v1",
        "m1-5-rrf-k60-page-context-v2",
        "m1-5-rrf-k60-page-context-v3",
        PAGE_SELECTOR_VERSION,
    }
)
PAGE_CONTEXT_SLOTS = 4
PDF_NEIGHBOR_ANCHOR_LIMIT = 4
STRUCTURED_CONTEXT_LIMIT = 7
_OBSERVATION_METRIC = re.compile(
    r"^\s*(?:what(?:\s+exact)?|how much)\s+(?P<metric>.+?)\s+"
    r"(?:did|does|was|were|is|has|had)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z]+(?:[-\u2013][a-z]+)*|\d+")
_QUESTION_WORD = re.compile(
    r"a|about|actual|also|an|and|any|as|at|by|can|company|describe|did|do|does|exact|for|from|"
    r"gave|give|had|has|have|how|in|into|is|it|its|many|move|much|of|on|or|reported|say|should|"
    r"than|that|the|their|there|they|this|to|use|was|were|what|when|where|which|who|why|with|year"
)
_REPORTED_QUANTITY = re.compile(
    r"[$€£]\s*\d|\b\d[\d.,]*\s*(?:%|million\b|billion\b|bps\b|MW\b|GW\b)", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class PdfNeighbor:
    """One immediate, eligible neighbor of a selected PDF passage."""

    anchor_id: UUID
    hit: LexicalSearchHit


def content_phrases(text: str) -> set[tuple[str, str]]:
    """Collect adjacent English content words for local passage ranking."""
    words = _WORD.findall(text.casefold())
    return {
        (left, right)
        for left, right in pairwise(words)
        if left.isalpha()
        and right.isalpha()
        and not (_QUESTION_WORD.fullmatch(left) or _QUESTION_WORD.fullmatch(right))
    }


def contains_reported_quantity(text: str) -> bool:
    """Recognize a numeric evidence signal without assigning it to a metric."""
    return _REPORTED_QUANTITY.search(text) is not None


def add_pdf_neighbor(
    question: str,
    selected: tuple[SelectedRetrievalCandidate, ...],
    neighbors: Sequence[PdfNeighbor],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[SelectedRetrievalCandidate, ...]:
    """Keep high-ranking evidence and admit a neighbor with an uncovered metric phrase."""
    if not neighbors:
        return selected
    query_phrases = content_phrases(question)
    if not query_phrases:
        return selected
    covered = [content_phrases(item.text) & query_phrases for item in selected]
    phrase_counts = Counter(phrase for phrases in covered for phrase in phrases)
    selected_ids = {item.chunk_id for item in selected}
    selected_texts = {" ".join(item.text.casefold().split()) for item in selected}
    anchors = {
        item.chunk_id: index for index, item in enumerate(selected[:PDF_NEIGHBOR_ANCHOR_LIMIT])
    }
    date = requested_observation_date(question)
    choices: list[tuple[int, int, LexicalSearchHit]] = []
    for neighbor in neighbors:
        index = anchors.get(neighbor.anchor_id)
        if index is None:
            continue
        anchor, hit = selected[index], neighbor.hit
        if (
            anchor.source_locator_kind is not SourceLocatorKind.PDF_PAGE
            or hit.source_locator_kind is not SourceLocatorKind.PDF_PAGE
            or (hit.document_version_id, hit.page_number)
            != (anchor.document_version_id, anchor.page_number)
            or hit.chunk_id in selected_ids
            or " ".join(hit.text.casefold().split()) in selected_texts
            or not contains_reported_quantity(hit.text)
            or (date is not None and not contains_observation_date(hit.text, date))
        ):
            continue
        document_phrases = {
            phrase
            for item, phrases in zip(selected, covered, strict=True)
            if item.document_version_id == hit.document_version_id
            for phrase in phrases
        }
        added = (content_phrases(hit.text) & query_phrases) - document_phrases
        if added:
            choices.append((-len(added), index, hit))

    structured_full = (
        sum(item.origin is RetrievalOrigin.STRUCTURED for item in selected)
        >= STRUCTURED_CONTEXT_LIMIT
    )
    for _, index, hit in sorted(choices, key=lambda item: (item[0], item[1], item[2].chunk_id)):
        kept = list(selected)
        if len(kept) >= chunk_limit or structured_full:
            remove = next(
                (
                    rank
                    for rank in range(len(selected) - 1, PDF_NEIGHBOR_ANCHOR_LIMIT - 1, -1)
                    if selected[rank].document_version_id != hit.document_version_id
                    and (not structured_full or selected[rank].origin is RetrievalOrigin.STRUCTURED)
                    and all(phrase_counts[phrase] > 1 for phrase in covered[rank])
                ),
                None,
            )
            if remove is None:
                continue
            kept.pop(remove)
        if sum(len(item.text) for item in kept) + len(hit.text) <= character_limit:
            kept.insert(index + 1, _structured(hit))
            return _fit(kept, chunk_limit, character_limit)
    return selected


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
