"""Select reported monetary evidence for two explicitly scoped company questions."""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import chain
from uuid import UUID

from investment_research.modules.search.contracts import (
    LexicalSearchHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.page_context import STRUCTURED_CONTEXT_LIMIT
from investment_research.modules.search.quarter_context import (
    QuarterPassage,
    ReportingQuarter,
    report_quarter,
)

_CLAUSE_BOUNDARY = re.compile(r"(?:,\s*|\s+)and\s+(?=what\s+did\b)", re.I)
_CLAUSE = re.compile(
    r"what did (?P<company>[\w$ .&'\u2019\-]+?) report (?:as )?"
    r"(?P<metric>[a-z][a-z -]+?) (?:in|for) (?P<quarter>Q[1-4]\s+(?:FY\s*)?20\d{2})\??",
    re.I,
)
_MONEY = re.compile(r"[$€£]\s*\d[\d,.]*\s*(?P<scale>million|billion|trillion|[mbt])\b", re.I)
_ACTUAL = re.compile(
    r"\b(?:recognized|generated|reported|recorded|delivered|posted|totaled|achieved)\b"
)
_FORECAST = re.compile(
    r"\b(?:expect(?:s|ed)?|guidance|outlook|forecast(?:s|ed)?|project(?:s|ed)?|target(?:s|ed)?)\b"
)
_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|[•▪●]|\n\s*\n")
_AMOUNT_AFTER = re.compile(r":?|(?:of|at|was|were|reached|totaled|stood at|hit)(?: a record)?")
_AMOUNT_BEFORE = re.compile(r"(?:of|in)?")
_METRIC_WINDOW = 120

type MetricScore = tuple[bool, bool, bool, int, int]


@dataclass(frozen=True, slots=True)
class CompanyMetric:
    company: str
    metric: str
    quarter: ReportingQuarter


def company_metric_comparison(question: str) -> tuple[CompanyMetric, ...]:
    """Require two self-contained company, metric, and period clauses."""
    parts = _CLAUSE_BOUNDARY.split(" ".join(question.split()))
    if len(parts) != 2:
        return ()
    requests: list[CompanyMetric] = []
    for part in parts:
        match = _CLAUSE.fullmatch(part)
        if match is None or not 2 <= len(match["metric"].split()) <= 8:
            return ()
        quarter = report_quarter(match["quarter"])
        if quarter is None:
            return ()
        requests.append(CompanyMetric(match["company"], match["metric"], quarter))
    return tuple(requests)


def _metric_scores(body: str, words: tuple[str, ...]) -> Iterable[MetricScore]:
    leading = re.compile(r"\b" + r"\s+".join(map(re.escape, words[:-1])) + r"\b")
    head_pattern = re.compile(rf"\b{re.escape(words[-1])}\b")
    exact = re.compile(r"\b" + r"\s+".join(map(re.escape, words)) + r"\b")

    for part in _BOUNDARY.split(unicodedata.normalize("NFKC", body)):
        sentence = " ".join(part.casefold().split())
        for anchor in leading.finditer(sentence):
            window = sentence[anchor.start() : anchor.start() + _METRIC_WINDOW]
            for head in head_pattern.finditer(window, anchor.end() - anchor.start()):
                for amount in _MONEY.finditer(window):
                    if amount.start() >= head.end():
                        gap = window[head.end() : amount.start()].strip()
                        relation = _AMOUNT_AFTER
                    elif amount.end() <= head.start():
                        gap = window[amount.end() : head.start()].strip()
                        relation = _AMOUNT_BEFORE
                    else:
                        continue
                    end = max(amount.end(), head.end())
                    scope = window[max(0, amount.start() - 45) : end + 25]
                    if relation.fullmatch(gap) and not _FORECAST.search(scope):
                        yield (
                            bool(_ACTUAL.search(scope)),
                            len(amount["scale"]) > 1,
                            bool(exact.search(window[:end])),
                            -len(gap.split()),
                            -end,
                        )


def best_company_metric_passage(
    metric: str, passages: Sequence[QuarterPassage]
) -> LexicalSearchHit | None:
    """Prefer nearby scaled amounts and affirmative reporting language."""
    words = tuple(word for word in metric.casefold().split() if word != "total")
    if len(words) < 2:
        return None
    scored = (
        (score, -len(passage.hit.text), passage.hit.chunk_id, passage.hit)
        for passage in passages
        if (score := max(_metric_scores(passage.hit.text, words), default=None)) is not None
    )
    best = max(scored, default=None, key=lambda entry: entry[:3])
    return best[3] if best is not None else None


def reserve_company_metric_passages(
    nominees: Sequence[LexicalSearchHit],
    selected: tuple[SelectedRetrievalCandidate, ...],
    *,
    chunk_limit: int,
    character_limit: int,
) -> tuple[SelectedRetrievalCandidate, ...]:
    """Keep the pair first, reusing existing lane provenance for selected chunks."""
    if len(nominees) != 2 or nominees[0].chunk_id == nominees[1].chunk_id:
        return selected
    if chunk_limit < 2 or sum(len(hit.text) for hit in nominees) > character_limit:
        return selected
    by_chunk = {hit.chunk_id: hit for hit in selected}
    promoted = (
        by_chunk[hit.chunk_id]
        if hit.chunk_id in by_chunk
        else SelectedRetrievalCandidate(
            **hit.model_dump(), origin=RetrievalOrigin.STRUCTURED, rank=1, selection_rank=1
        )
        for hit in nominees
    )
    result: list[SelectedRetrievalCandidate] = []
    seen: set[UUID] = set()
    characters = structured = 0
    for hit in chain(promoted, selected):
        if len(result) == chunk_limit:
            break
        if hit.chunk_id in seen or characters + len(hit.text) > character_limit:
            continue
        if hit.origin is RetrievalOrigin.STRUCTURED:
            if structured >= STRUCTURED_CONTEXT_LIMIT:
                continue
            structured += 1
        seen.add(hit.chunk_id)
        characters += len(hit.text)
        result.append(hit.model_copy(update={"selection_rank": len(result) + 1}))
    return tuple(result)
